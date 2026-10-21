from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.broadcasts import (
    BroadcastGateway,
    BroadcastService,
    BroadcastWorker,
    Clock,
)
from terricon_events_bot.application.delivery import TelegramSendError
from terricon_events_bot.domain.enums import (
    BroadcastAudience,
    BroadcastStatus,
    DeliveryStatus,
    Locale,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    Broadcast,
    BroadcastDelivery,
    NotificationOutbox,
    User,
)

pytestmark = pytest.mark.postgres


class FrozenClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class FakeGateway:
    def __init__(self, outcomes: list[int | TelegramSendError] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[str, int, str]] = []
        self.reports: list[tuple[int, str]] = []
        self.next_id = 100

    async def send_message(self, telegram_id: int, text_value: str) -> int:
        return await self._send("text", telegram_id, text_value)

    async def send_photo(self, telegram_id: int, telegram_file_id: str) -> int:
        return await self._send("photo", telegram_id, telegram_file_id)

    async def send_report(self, admin_telegram_id: int, text_value: str) -> int:
        self.reports.append((admin_telegram_id, text_value))
        self.next_id += 1
        return self.next_id

    async def _send(self, kind: str, telegram_id: int, value: str) -> int:
        self.calls.append((kind, telegram_id, value))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, TelegramSendError):
                raise outcome
            return outcome
        self.next_id += 1
        return self.next_id


class LockCheckingGateway(FakeGateway):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__()
        self._session_factory = session_factory

    async def send_report(self, admin_telegram_id: int, text_value: str) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                await session.scalar(select(Broadcast).with_for_update(nowait=True))
        return await super().send_report(admin_telegram_id, text_value)


class FailingReportGateway(FakeGateway):
    async def send_report(self, admin_telegram_id: int, text_value: str) -> int:
        raise TelegramSendError("report_network", transient=True)


@pytest.fixture
async def broadcast_database(
    migrated_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users, broadcasts RESTART IDENTITY CASCADE"))
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE users, broadcasts RESTART IDENTITY CASCADE")
            )
        await engine.dispose()


async def add_user(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    locale: Locale,
    *,
    active: bool = True,
) -> int:
    async with session_factory() as session:
        async with session.begin():
            user = User(
                telegram_id=telegram_id,
                locale=locale,
                is_active=active,
                access_granted=True,
                onboarding_completed=True,
            )
            session.add(user)
            await session.flush()
            return user.id


@pytest.mark.asyncio
async def test_broadcast_materializes_localized_audience_and_concurrent_workers(
    broadcast_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = broadcast_database
    await add_user(session_factory, 7_200_001, Locale.RU)
    await add_user(session_factory, 7_200_002, Locale.KZ)
    await add_user(session_factory, 7_200_003, Locale.RU, active=False)
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = BroadcastService(session_factory, clock=cast(Clock, clock))
    draft = await service.create_draft(9_000_001, BroadcastAudience.BOTH)
    await service.set_body(draft.broadcast_id, Locale.RU, "RU body")
    await service.set_body(draft.broadcast_id, Locale.KZ, "KZ body")
    report = await service.confirm(draft.broadcast_id)
    gateway = FakeGateway()
    workers = [
        BroadcastWorker(
            session_factory,
            cast(BroadcastGateway, gateway),
            clock=cast(Clock, clock),
        )
        for _ in range(2)
    ]

    results = await asyncio.gather(*(worker.run_batch(limit=1) for worker in workers))
    completed = await service.report(draft.broadcast_id)

    assert report.total == 2
    assert sum(result.claimed for result in results) == 2
    assert completed is not None
    assert completed.status is BroadcastStatus.COMPLETED
    assert completed.sent == 2
    assert len(gateway.reports) == 1
    assert "Доставлено: 2" in gateway.reports[0][1]
    assert {(telegram_id, body) for _, telegram_id, body in gateway.calls} == {
        (7_200_001, "RU body"),
        (7_200_002, "KZ body"),
    }


@pytest.mark.asyncio
async def test_broadcast_resumes_after_partial_delivery_and_stale_lease(
    broadcast_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = broadcast_database
    await add_user(session_factory, 7_200_004, Locale.RU)
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = BroadcastService(session_factory, clock=cast(Clock, clock))
    draft = await service.create_draft(9_000_001, BroadcastAudience.RU)
    body = "A" * 5000
    await service.set_body(draft.broadcast_id, Locale.RU, body)
    await service.set_photo(draft.broadcast_id, "photo-id")
    await service.confirm(draft.broadcast_id)
    first_gateway = FakeGateway(
        [
            11,
            12,
            TelegramSendError("network", transient=True),
        ]
    )
    first_worker = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, first_gateway),
        clock=cast(Clock, clock),
    )

    first = await first_worker.run_batch()
    async with session_factory() as session:
        delivery = await session.scalar(select(BroadcastDelivery))
        assert delivery is not None
        assert delivery.telegram_message_ids == [11, 12]
        assert delivery.status is DeliveryStatus.RETRY_WAIT

    clock.current += timedelta(minutes=10)
    second_gateway = FakeGateway([13])
    restarted = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, second_gateway),
        clock=cast(Clock, clock),
    )
    second = await restarted.run_batch()

    assert first.retrying == 1
    assert second.sent == 1
    assert second_gateway.calls == [("text", 7_200_004, "A" * 1000)]
    assert len(second_gateway.reports) == 1
    report = await service.report(draft.broadcast_id)
    assert report is not None and report.transient == 1
    assert "временных ошибок: 1" in second_gateway.reports[0][1]


@pytest.mark.asyncio
async def test_telegram_block_closes_all_pending_broadcasts_for_user(
    broadcast_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = broadcast_database
    user_id = await add_user(session_factory, 7_200_005, Locale.RU)
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = BroadcastService(session_factory, clock=cast(Clock, clock))
    broadcast_ids: list[int] = []
    for body in ("First", "Second"):
        draft = await service.create_draft(9_000_001 + len(broadcast_ids), BroadcastAudience.RU)
        await service.set_body(draft.broadcast_id, Locale.RU, body)
        await service.confirm(draft.broadcast_id)
        broadcast_ids.append(draft.broadcast_id)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                NotificationOutbox(
                    user_id=user_id,
                    batch_id=uuid4(),
                    batch_key="broadcast-block-active-outbox",
                    status=DeliveryStatus.PROCESSING,
                    payload={"version": 1, "chunks": []},
                    available_at=clock.current,
                    locked_at=clock.current,
                )
            )
    gateway = FakeGateway([TelegramSendError("forbidden", blocked=True)])
    worker = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, gateway),
        clock=cast(Clock, clock),
    )

    result = await worker.run_batch(limit=1)

    async with session_factory() as session:
        user = await session.get(User, user_id)
        deliveries = (await session.scalars(select(BroadcastDelivery))).all()
        broadcasts = (await session.scalars(select(Broadcast))).all()
        outbox = await session.scalar(select(NotificationOutbox))
    assert result.blocked == 1
    assert user is not None and not user.is_active
    assert {delivery.status for delivery in deliveries} == {DeliveryStatus.BLOCKED}
    assert {broadcast.status for broadcast in broadcasts} == {BroadcastStatus.COMPLETED}
    assert outbox is not None and outbox.status is DeliveryStatus.BLOCKED
    assert outbox.locked_at is None


@pytest.mark.asyncio
async def test_processing_delivery_without_lease_is_recovered_after_upgrade(
    broadcast_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = broadcast_database
    await add_user(session_factory, 7_200_006, Locale.RU)
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = BroadcastService(session_factory, clock=cast(Clock, clock))
    draft = await service.create_draft(9_000_010, BroadcastAudience.RU)
    await service.set_body(draft.broadcast_id, Locale.RU, "Recovered")
    await service.confirm(draft.broadcast_id)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(BroadcastDelivery).values(
                    status=DeliveryStatus.PROCESSING,
                    locked_at=None,
                )
            )
            await session.execute(
                update(Broadcast)
                .where(Broadcast.id == draft.broadcast_id)
                .values(status=BroadcastStatus.RUNNING)
            )
    gateway = FakeGateway()
    worker = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, gateway),
        clock=cast(Clock, clock),
    )

    result = await worker.run_batch()

    assert result.sent == 1
    assert gateway.calls == [("text", 7_200_006, "Recovered")]


@pytest.mark.asyncio
async def test_completed_report_is_sent_without_holding_broadcast_transaction(
    broadcast_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = broadcast_database
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = BroadcastService(session_factory, clock=cast(Clock, clock))
    draft = await service.create_draft(9_000_020, BroadcastAudience.RU)
    await service.set_body(draft.broadcast_id, Locale.RU, "No recipients")
    await service.confirm(draft.broadcast_id)
    gateway = LockCheckingGateway(session_factory)
    worker = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, gateway),
        clock=cast(Clock, clock),
    )

    await worker.run_batch()

    async with session_factory() as session:
        broadcast = await session.get(Broadcast, draft.broadcast_id)
    assert len(gateway.reports) == 1
    assert broadcast is not None and broadcast.report_sent_at == clock.current
    assert broadcast.report_locked_at is None


@pytest.mark.asyncio
async def test_failed_completed_report_is_retried_after_stale_lease(
    broadcast_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = broadcast_database
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = BroadcastService(session_factory, clock=cast(Clock, clock))
    draft = await service.create_draft(9_000_021, BroadcastAudience.RU)
    await service.set_body(draft.broadcast_id, Locale.RU, "Retry report")
    await service.confirm(draft.broadcast_id)

    failed_worker = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, FailingReportGateway()),
        clock=cast(Clock, clock),
    )
    await failed_worker.run_batch()
    gateway = FakeGateway()
    restarted = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, gateway),
        clock=cast(Clock, clock),
    )
    await restarted.run_batch()
    assert gateway.reports == []

    clock.current += timedelta(minutes=6)
    await restarted.run_batch()

    assert len(gateway.reports) == 1


@pytest.mark.asyncio
async def test_single_locale_broadcast_keeps_confirmed_audience_language(
    broadcast_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = broadcast_database
    user_id = await add_user(session_factory, 7_200_007, Locale.RU)
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = BroadcastService(session_factory, clock=cast(Clock, clock))
    draft = await service.create_draft(9_000_022, BroadcastAudience.RU)
    await service.set_body(draft.broadcast_id, Locale.RU, "RU audience")
    await service.confirm(draft.broadcast_id)
    async with session_factory() as session:
        async with session.begin():
            user = await session.get(User, user_id, with_for_update=True)
            assert user is not None
            user.locale = Locale.KZ
    gateway = FakeGateway()
    worker = BroadcastWorker(
        session_factory,
        cast(BroadcastGateway, gateway),
        clock=cast(Clock, clock),
    )

    result = await worker.run_batch()

    assert result.sent == 1
    assert gateway.calls == [("text", 7_200_007, "RU audience")]
