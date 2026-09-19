from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.broadcasts import BroadcastService
from terricon_events_bot.application.feedback import (
    Clock,
    FeedbackBlockedError,
    FeedbackRateLimitedError,
    FeedbackService,
    FeedbackStateError,
)
from terricon_events_bot.application.subscriptions import SubscriptionMode, SubscriptionService
from terricon_events_bot.application.users import UserService
from terricon_events_bot.domain.enums import (
    AccessMode,
    BroadcastAudience,
    FeedbackKind,
    Locale,
    TicketStatus,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    BroadcastDelivery,
    FeedbackMessage,
    FeedbackTicket,
    FsmState,
    SubscriptionSettings,
    User,
)

pytestmark = pytest.mark.postgres


class FrozenClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


@pytest.fixture
async def feedback_database(
    migrated_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
        await engine.dispose()


async def add_user(session_factory: async_sessionmaker[AsyncSession], telegram_id: int) -> int:
    async with session_factory() as session:
        async with session.begin():
            user = User(
                telegram_id=telegram_id,
                display_name="Safe Name",
                username="safe_user",
                locale=Locale.RU,
                access_granted=True,
                onboarding_completed=True,
            )
            session.add(user)
            await session.flush()
            return user.id


@pytest.mark.asyncio
async def test_feedback_wizard_rate_limit_block_and_two_way_dialogue(
    feedback_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = feedback_database
    user_id = await add_user(session_factory, 7_100_001)
    admin_id = await add_user(session_factory, 7_100_002)
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = FeedbackService(session_factory, clock=cast(Clock, clock))

    await service.begin(user_id, FeedbackKind.ERROR)
    await service.set_text(user_id, "Broken screen")
    preview = await service.set_photo(user_id, "photo-file-id")
    notification = await service.confirm(user_id)

    assert preview.state == "preview"
    assert notification.body == "Broken screen"
    assert notification.telegram_file_id == "photo-file-id"
    await service.attach_admin_message(notification.ticket_id, -100500, 77)

    await service.begin_admin_reply(admin_id, notification.ticket_id)
    assert await service.pending_admin_reply(admin_id) == notification.ticket_id
    await service.complete_admin_reply(admin_id, notification.ticket_id, "Please retry", 88)
    reply = await service.add_user_reply(user_id, body="It works now")
    assert reply.continuation

    for kind in (FeedbackKind.IDEA, FeedbackKind.OTHER):
        await service.begin(user_id, kind)
        await service.set_text(user_id, f"Ticket {kind.value}")
        await service.set_photo(user_id, None)
        await service.confirm(user_id)

    await service.begin(user_id, FeedbackKind.ERROR)
    await service.set_text(user_id, "Fourth ticket")
    await service.set_photo(user_id, None)
    with pytest.raises(FeedbackRateLimitedError):
        await service.confirm(user_id)

    await service.set_blocked(
        notification.ticket_id,
        blocked=True,
        admin_telegram_id=7_100_002,
    )
    with pytest.raises(FeedbackBlockedError):
        await service.begin(user_id, FeedbackKind.OTHER)

    ticket = await service.ticket(notification.ticket_id)
    assert ticket is not None
    assert ticket.status is TicketStatus.AWAITING_ADMIN
    assert len(ticket.messages) == 3


@pytest.mark.asyncio
async def test_feedback_cleanup_and_profile_deletion_remove_private_data(
    feedback_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = feedback_database
    user_id = await add_user(session_factory, 7_100_003)
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = FeedbackService(session_factory, clock=cast(Clock, clock))
    await service.begin(user_id, FeedbackKind.OTHER)
    await service.set_text(user_id, "Private text")
    await service.set_photo(user_id, "private-file-id")
    notification = await service.confirm(user_id)
    await service.close(notification.ticket_id)
    clock.current += timedelta(days=91)

    assert await service.cleanup(90) == 1

    await service.begin(user_id, FeedbackKind.IDEA)
    await service.set_text(user_id, "Delete me")
    await service.set_photo(user_id, None)
    await service.confirm(user_id)
    await service.begin(user_id, FeedbackKind.ERROR)
    await SubscriptionService(session_factory).replace(user_id, SubscriptionMode.ALL)
    broadcast_service = BroadcastService(session_factory)
    broadcast = await broadcast_service.create_draft(7_100_099, BroadcastAudience.RU)
    await broadcast_service.set_body(broadcast.broadcast_id, Locale.RU, "Service message")
    await broadcast_service.confirm(broadcast.broadcast_id)
    users = UserService(
        session_factory,
        access_mode=AccessMode.PUBLIC,
        allowed_telegram_ids=frozenset(),
        admin_telegram_ids=frozenset({7_100_099}),
    )
    await users.delete_profile(user_id)

    async with session_factory() as session:
        assert await session.get(User, user_id) is None
        assert await session.scalar(select(func.count()).select_from(FeedbackTicket)) == 0
        assert await session.scalar(select(func.count()).select_from(FeedbackMessage)) == 0
        assert await session.scalar(select(func.count()).select_from(FsmState)) == 0
        assert await session.scalar(select(func.count()).select_from(SubscriptionSettings)) == 0
        assert await session.scalar(select(func.count()).select_from(BroadcastDelivery)) == 0


@pytest.mark.asyncio
async def test_user_reply_requires_ticket_waiting_for_user(
    feedback_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = feedback_database
    user_id = await add_user(session_factory, 7_100_004)
    service = FeedbackService(session_factory)

    with pytest.raises(FeedbackStateError):
        await service.add_user_reply(user_id, body="Unexpected")
