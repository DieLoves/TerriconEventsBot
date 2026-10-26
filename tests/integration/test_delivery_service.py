from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.delivery import (
    Clock,
    DeliveryWorker,
    DigestRenderer,
    OutboxService,
    QuietHours,
    TelegramSendError,
)
from terricon_events_bot.application.subscriptions import (
    SubscriptionMode,
    SubscriptionService,
)
from terricon_events_bot.domain.enums import (
    BroadcastAudience,
    BroadcastStatus,
    CategorySlug,
    ClassificationStatus,
    DeliveryStatus,
    Locale,
    NotificationType,
    SourceTheme,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    Broadcast,
    BroadcastDelivery,
    DomainChange,
    Event,
    EventClassification,
    EventLocalization,
    NotificationDelivery,
    NotificationOutbox,
    User,
)
from terricon_events_bot.localization import LocalizationCatalog

pytestmark = pytest.mark.postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALMATY = ZoneInfo("Asia/Almaty")


class FrozenClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakeGateway:
    def __init__(self, outcomes: list[int | TelegramSendError] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[int, str]] = []
        self._next_message_id = 1000

    async def send_message(self, telegram_id: int, message: str) -> int:
        self.calls.append((telegram_id, message))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, TelegramSendError):
                raise outcome
            return outcome
        self._next_message_id += 1
        return self._next_message_id


class SlowGateway(FakeGateway):
    async def send_message(self, telegram_id: int, message: str) -> int:
        await asyncio.sleep(0.05)
        return await super().send_message(telegram_id, message)


@pytest.fixture
async def delivery_database(
    migrated_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users, events RESTART IDENTITY CASCADE"))
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE users, events RESTART IDENTITY CASCADE"))
        await engine.dispose()


def outbox_service(
    session_factory: async_sessionmaker[AsyncSession],
    clock: FrozenClock,
    *,
    text_limit: int = 4000,
) -> OutboxService:
    renderer = DigestRenderer(
        LocalizationCatalog.load(PROJECT_ROOT / "locales"),
        timezone=ALMATY,
        text_limit=text_limit,
    )
    return OutboxService(
        session_factory,
        renderer,
        QuietHours(ALMATY, time(22), time(9)),
        clock=cast(Clock, clock),
    )


async def add_user(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    mode: SubscriptionMode,
    categories: tuple[CategorySlug, ...] = (),
) -> int:
    async with session_factory() as session:
        async with session.begin():
            user = User(
                telegram_id=telegram_id,
                locale=Locale.RU,
                onboarding_completed=True,
                access_granted=True,
            )
            session.add(user)
            await session.flush()
            user_id = user.id
    await SubscriptionService(session_factory).replace(user_id, mode, categories)
    return user_id


async def add_event(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: int,
    title: str,
    starts_at: datetime,
    *,
    event_type: str | None = None,
) -> int:
    async with session_factory() as session:
        async with session.begin():
            event = Event(
                source_id=source_id,
                source_theme=SourceTheme.IT,
                starts_at=starts_at,
                event_type=event_type,
                revision=1,
            )
            session.add(event)
            await session.flush()
            session.add(
                EventLocalization(
                    event_id=event.id,
                    locale=Locale.RU,
                    title=title,
                    content_hash=f"hash-{source_id}",
                )
            )
            return event.id


async def add_change(
    session_factory: async_sessionmaker[AsyncSession],
    event_id: int,
    revision: int,
    notification_type: NotificationType,
    *,
    old_categories: tuple[CategorySlug, ...] = (),
    new_categories: tuple[CategorySlug, ...] = (),
    change_data: dict[str, object] | None = None,
) -> int:
    async with session_factory() as session:
        async with session.begin():
            change = DomainChange(
                event_id=event_id,
                event_revision=revision,
                notification_type=notification_type,
                old_categories=[item.value for item in old_categories],
                new_categories=[item.value for item in new_categories],
                change_data=change_data or {},
            )
            session.add(change)
            await session.flush()
            return change.id


@pytest.mark.asyncio
async def test_materialization_uses_category_union_and_is_idempotent(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    all_user = await add_user(session_factory, 4100001, SubscriptionMode.ALL)
    old_user = await add_user(
        session_factory,
        4100002,
        SubscriptionMode.CATEGORIES,
        (CategorySlug.AI_DATA, CategorySlug.MARKETING_SALES),
    )
    new_user = await add_user(
        session_factory,
        4100003,
        SubscriptionMode.CATEGORIES,
        (CategorySlug.MARKETING_SALES,),
    )
    await add_user(
        session_factory,
        4100004,
        SubscriptionMode.CATEGORIES,
        (CategorySlug.DESIGN_CREATIVE,),
    )
    event_id = await add_event(
        session_factory, 5100001, "Changed event", datetime(2030, 1, 5, tzinfo=UTC)
    )
    await add_change(
        session_factory,
        event_id,
        1,
        NotificationType.IMPORTANT_CHANGE,
        old_categories=(CategorySlug.AI_DATA,),
        new_categories=(CategorySlug.MARKETING_SALES,),
        change_data={"starts_at": {"old": "a", "new": "b"}},
    )
    clock = FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    service = outbox_service(session_factory, clock)

    concurrent = await asyncio.gather(service.materialize(), service.materialize())
    second = await service.materialize()

    async with session_factory() as session:
        deliveries = (
            await session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))
        ).all()
        changes = (await session.scalars(select(DomainChange))).all()

    assert sum(item.changes for item in concurrent) == 1
    assert sum(item.deliveries for item in concurrent) == 3
    assert sum(item.outboxes for item in concurrent) == 3
    assert second.changes == second.deliveries == second.outboxes == 0
    assert {item.user_id for item in deliveries} == {all_user, old_user, new_user}
    assert len(deliveries) == 3
    assert changes[0].materialized_at == clock.now()


@pytest.mark.asyncio
async def test_materialization_waits_for_categories_from_pending_classification(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    old_user = await add_user(
        session_factory,
        4100101,
        SubscriptionMode.CATEGORIES,
        (CategorySlug.AI_DATA,),
    )
    new_user = await add_user(
        session_factory,
        4100102,
        SubscriptionMode.CATEGORIES,
        (CategorySlug.MARKETING_SALES,),
    )
    event_id = await add_event(
        session_factory,
        5100101,
        "Reclassified event",
        datetime(2030, 1, 5, tzinfo=UTC),
    )
    change_id = await add_change(
        session_factory,
        event_id,
        2,
        NotificationType.IMPORTANT_CHANGE,
        old_categories=(CategorySlug.AI_DATA,),
        new_categories=(CategorySlug.AI_DATA,),
        change_data={"titles": {"old": "Old", "new": "New"}},
    )
    now = datetime(2030, 1, 1, 7, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                EventClassification(
                    event_id=event_id,
                    status=ClassificationStatus.PENDING,
                    semantic_hash="pending-hash",
                    first_attempt_at=now,
                    next_attempt_at=now,
                    result_metadata={"queued_revision": 2},
                )
            )
    service = outbox_service(session_factory, FrozenClock(now))

    pending = await service.materialize()

    async with session_factory() as session:
        change = await session.get(DomainChange, change_id)
        assert change is not None and change.materialized_at is None
    async with session_factory() as session:
        async with session.begin():
            classification = await session.get(
                EventClassification,
                event_id,
                with_for_update=True,
            )
            assert classification is not None
            classification.status = ClassificationStatus.COMPLETED
            change = await session.get(DomainChange, change_id, with_for_update=True)
            assert change is not None
            change.new_categories = [CategorySlug.MARKETING_SALES.value]

    completed = await service.materialize()

    async with session_factory() as session:
        recipient_ids = set(await session.scalars(select(NotificationDelivery.user_id)))

    assert pending.changes == pending.deliveries == pending.outboxes == 0
    assert completed.changes == 1
    assert completed.deliveries == completed.outboxes == 2
    assert recipient_ids == {old_user, new_user}


@pytest.mark.asyncio
async def test_materialization_skips_changes_for_events_that_already_ended(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    user_id = await add_user(session_factory, 4100201, SubscriptionMode.ALL)
    # 2030-01-10 12:00 Almaty; "today" starts at 2030-01-09 19:00 UTC.
    clock = FrozenClock(datetime(2030, 1, 10, 7, tzinfo=UTC))
    ended = await add_event(
        session_factory, 5100201, "Yesterday event", datetime(2030, 1, 9, 12, tzinfo=UTC)
    )
    today = await add_event(
        session_factory, 5100202, "Morning event", datetime(2030, 1, 10, 3, tzinfo=UTC)
    )
    skipped_changes = [
        await add_change(
            session_factory,
            ended,
            revision,
            notification_type,
            new_categories=(CategorySlug.AI_DATA,),
            change_data={"registration_url": {"old": "https://a.example/", "new": None}},
        )
        for revision, notification_type in enumerate(
            (
                NotificationType.NEW_EVENT,
                NotificationType.IMPORTANT_CHANGE,
                NotificationType.CANCELLATION,
            ),
            start=1,
        )
    ]
    await add_change(
        session_factory,
        today,
        2,
        NotificationType.IMPORTANT_CHANGE,
        new_categories=(CategorySlug.AI_DATA,),
        change_data={"address": {"old": "A", "new": "B"}},
    )

    result = await outbox_service(session_factory, clock).materialize()

    async with session_factory() as session:
        deliveries = (await session.scalars(select(NotificationDelivery))).all()
        skipped = (
            await session.scalars(select(DomainChange).where(DomainChange.id.in_(skipped_changes)))
        ).all()

    assert result.changes == 4
    assert [(item.user_id, item.event_id) for item in deliveries] == [(user_id, today)]
    assert all(change.materialized_at == clock.now() for change in skipped)


@pytest.mark.asyncio
async def test_change_without_recipients_is_not_replayed_to_future_subscriber(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    event_id = await add_event(
        session_factory, 5100002, "Old event", datetime(2030, 1, 5, tzinfo=UTC)
    )
    await add_change(
        session_factory,
        event_id,
        1,
        NotificationType.NEW_EVENT,
        new_categories=(CategorySlug.OTHER,),
    )
    service = outbox_service(session_factory, FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC)))

    result = await service.materialize()
    await add_user(session_factory, 4100005, SubscriptionMode.ALL)
    repeated = await service.materialize()

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(NotificationDelivery))

    assert result == result.__class__(changes=1, deliveries=0, outboxes=0)
    assert repeated.changes == 0
    assert count == 0


@pytest.mark.asyncio
async def test_quiet_hours_merge_changes_and_deduplicate_event_in_one_batch(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    user_id = await add_user(session_factory, 4100006, SubscriptionMode.ALL)
    event_id = await add_event(
        session_factory, 5100003, "Night event", datetime(2030, 1, 5, tzinfo=UTC)
    )
    clock = FrozenClock(datetime(2030, 1, 1, 18, tzinfo=UTC))  # 23:00 Almaty
    service = outbox_service(session_factory, clock)
    await add_change(
        session_factory,
        event_id,
        1,
        NotificationType.NEW_EVENT,
        new_categories=(CategorySlug.OTHER,),
    )
    await service.materialize()
    await add_change(
        session_factory,
        event_id,
        2,
        NotificationType.IMPORTANT_CHANGE,
        old_categories=(CategorySlug.OTHER,),
        new_categories=(CategorySlug.OTHER,),
        change_data={"titles": {"old": "A", "new": "B"}},
    )

    second = await service.materialize()

    async with session_factory() as session:
        outboxes = (
            await session.scalars(
                select(NotificationOutbox).where(NotificationOutbox.user_id == user_id)
            )
        ).all()
        deliveries = (
            await session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.user_id == user_id)
            )
        ).all()

    assert second.outboxes == 0
    assert len(outboxes) == 1
    assert outboxes[0].available_at == datetime(2030, 1, 2, 4, tzinfo=UTC)
    assert len(deliveries) == 2
    chunks = outboxes[0].payload["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["text"].count("Night event") == 1
    assert sorted(chunks[0]["delivery_ids"]) == sorted(item.id for item in deliveries)


@pytest.mark.asyncio
async def test_worker_resumes_after_transient_failure_without_resending_acked_chunk(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    await add_user(session_factory, 4100007, SubscriptionMode.ALL)
    clock = FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    for offset, letter in enumerate(("A", "B"), start=1):
        event_id = await add_event(
            session_factory,
            5100010 + offset,
            letter * 60,
            datetime(2030, 1, 5 + offset, tzinfo=UTC),
        )
        await add_change(
            session_factory,
            event_id,
            1,
            NotificationType.NEW_EVENT,
            new_categories=(CategorySlug.OTHER,),
        )
    await outbox_service(session_factory, clock, text_limit=90).materialize()
    gateway = FakeGateway([101, TelegramSendError("network", transient=True), 102])
    worker = DeliveryWorker(session_factory, gateway, clock=cast(Clock, clock))

    first = await worker.run_batch()
    clock.advance(timedelta(minutes=1))
    second = await worker.run_batch()

    async with session_factory() as session:
        outbox = await session.scalar(select(NotificationOutbox))
        statuses = (
            await session.scalars(
                select(NotificationDelivery.status).order_by(NotificationDelivery.id)
            )
        ).all()

    assert first.retrying == 1
    assert second.sent == 1
    assert len(gateway.calls) == 3
    assert gateway.calls[0][1] != gateway.calls[1][1]
    assert gateway.calls[1][1] == gateway.calls[2][1]
    assert outbox is not None
    assert outbox.status is DeliveryStatus.SENT
    assert outbox.telegram_message_ids == [101, 102]
    assert statuses == [DeliveryStatus.SENT, DeliveryStatus.SENT]


@pytest.mark.asyncio
async def test_forbidden_deactivates_user_and_closes_all_pending_delivery(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    user_id = await add_user(session_factory, 4100008, SubscriptionMode.ALL)
    clock = FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    service = outbox_service(session_factory, clock)
    for offset in range(2):
        event_id = await add_event(
            session_factory,
            5100020 + offset,
            f"Blocked {offset}",
            datetime(2030, 1, 5 + offset, tzinfo=UTC),
        )
        await add_change(
            session_factory,
            event_id,
            1,
            NotificationType.NEW_EVENT,
            new_categories=(CategorySlug.OTHER,),
        )
        await service.materialize()
    async with session_factory() as session:
        async with session.begin():
            broadcast = Broadcast(
                created_by_telegram_id=9990001,
                audience=BroadcastAudience.RU,
                status=BroadcastStatus.READY,
                total_count=1,
            )
            session.add(broadcast)
            await session.flush()
            session.add(
                BroadcastDelivery(
                    broadcast_id=broadcast.id,
                    user_id=user_id,
                    status=DeliveryStatus.PENDING,
                    next_attempt_at=clock.now(),
                )
            )
            broadcast_id = broadcast.id
    worker = DeliveryWorker(
        session_factory,
        FakeGateway([TelegramSendError("forbidden", blocked=True)]),
        clock=cast(Clock, clock),
    )

    result = await worker.run_batch(limit=1)

    async with session_factory() as session:
        user = await session.get(User, user_id)
        outbox_statuses = (await session.scalars(select(NotificationOutbox.status))).all()
        delivery_statuses = (await session.scalars(select(NotificationDelivery.status))).all()
        broadcast = await session.get(Broadcast, broadcast_id)
        broadcast_delivery_status = await session.scalar(
            select(BroadcastDelivery.status).where(BroadcastDelivery.broadcast_id == broadcast_id)
        )

    assert result.blocked == 1
    assert user is not None and not user.is_active
    assert outbox_statuses == [DeliveryStatus.BLOCKED, DeliveryStatus.BLOCKED]
    assert delivery_statuses == [DeliveryStatus.BLOCKED, DeliveryStatus.BLOCKED]
    assert broadcast is not None
    assert broadcast.status is BroadcastStatus.COMPLETED
    assert broadcast.failed_count == 1
    assert broadcast_delivery_status is DeliveryStatus.BLOCKED


@pytest.mark.asyncio
async def test_permanent_error_is_failed_once_and_never_retried(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    await add_user(session_factory, 4100009, SubscriptionMode.ALL)
    clock = FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    event_id = await add_event(
        session_factory, 5100029, "Permanent", datetime(2030, 1, 5, tzinfo=UTC)
    )
    await add_change(
        session_factory,
        event_id,
        1,
        NotificationType.NEW_EVENT,
        new_categories=(CategorySlug.OTHER,),
    )
    await outbox_service(session_factory, clock).materialize()
    gateway = FakeGateway([TelegramSendError("bad_request")])
    worker = DeliveryWorker(session_factory, gateway, clock=cast(Clock, clock))

    first = await worker.run_batch()
    clock.advance(timedelta(days=1))
    second = await worker.run_batch()

    async with session_factory() as session:
        outbox_status = await session.scalar(select(NotificationOutbox.status))
        delivery_status = await session.scalar(select(NotificationDelivery.status))

    assert first.failed == 1
    assert second.claimed == 0
    assert len(gateway.calls) == 1
    assert outbox_status is DeliveryStatus.FAILED
    assert delivery_status is DeliveryStatus.FAILED


@pytest.mark.asyncio
async def test_incomplete_payload_fails_all_unrepresented_deliveries(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    await add_user(session_factory, 4100013, SubscriptionMode.ALL)
    clock = FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    for offset in range(2):
        event_id = await add_event(
            session_factory,
            5100041 + offset,
            f"Incomplete {offset}",
            datetime(2030, 1, 5 + offset, tzinfo=UTC),
        )
        await add_change(
            session_factory,
            event_id,
            1,
            NotificationType.NEW_EVENT,
            new_categories=(CategorySlug.OTHER,),
        )
    await outbox_service(session_factory, clock).materialize()
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(select(NotificationOutbox).with_for_update())
            delivery_ids = tuple(
                (
                    await session.scalars(
                        select(NotificationDelivery.id).order_by(NotificationDelivery.id)
                    )
                ).all()
            )
            assert outbox is not None
            outbox.payload = {
                "version": 1,
                "chunks": [{"text": "partial", "delivery_ids": [delivery_ids[0]]}],
            }

    result = await DeliveryWorker(
        session_factory, FakeGateway(), clock=cast(Clock, clock)
    ).run_batch()

    async with session_factory() as session:
        outbox = await session.scalar(select(NotificationOutbox))
        deliveries = (
            await session.execute(
                select(NotificationDelivery.status, NotificationDelivery.last_error_code).order_by(
                    NotificationDelivery.id
                )
            )
        ).all()

    assert result.failed == 1
    assert outbox is not None
    assert outbox.status is DeliveryStatus.FAILED
    assert outbox.locked_at is None
    assert deliveries == [
        (DeliveryStatus.SENT, None),
        (DeliveryStatus.FAILED, "incomplete_payload"),
    ]


@pytest.mark.asyncio
async def test_concurrent_workers_claim_each_outbox_once(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    clock = FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    service = outbox_service(session_factory, clock)
    for offset in range(2):
        await add_user(session_factory, 4100010 + offset, SubscriptionMode.ALL)
    for offset in range(2):
        event_id = await add_event(
            session_factory,
            5100030 + offset,
            f"Concurrent {offset}",
            datetime(2030, 1, 5 + offset, tzinfo=UTC),
        )
        await add_change(
            session_factory,
            event_id,
            1,
            NotificationType.NEW_EVENT,
            new_categories=(CategorySlug.OTHER,),
        )
    await service.materialize()
    gateway = SlowGateway()
    workers = (
        DeliveryWorker(session_factory, gateway, clock=cast(Clock, clock)),
        DeliveryWorker(session_factory, gateway, clock=cast(Clock, clock)),
    )

    results = await asyncio.gather(*(worker.run_batch(limit=1) for worker in workers))

    async with session_factory() as session:
        statuses = (await session.scalars(select(NotificationOutbox.status))).all()

    assert sum(result.claimed for result in results) == 2
    assert len(gateway.calls) == 2
    assert len({telegram_id for telegram_id, _ in gateway.calls}) == 2
    assert statuses == [DeliveryStatus.SENT, DeliveryStatus.SENT]


@pytest.mark.parametrize("locked_at", [None, datetime(2030, 1, 1, 6, 50, tzinfo=UTC)])
@pytest.mark.asyncio
async def test_stale_processing_lease_is_recovered_after_restart(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    locked_at: datetime | None,
) -> None:
    _, session_factory = delivery_database
    await add_user(session_factory, 4100012, SubscriptionMode.ALL)
    clock = FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    event_id = await add_event(
        session_factory, 5100040, "Restart", datetime(2030, 1, 5, tzinfo=UTC)
    )
    await add_change(
        session_factory,
        event_id,
        1,
        NotificationType.NEW_EVENT,
        new_categories=(CategorySlug.OTHER,),
    )
    await outbox_service(session_factory, clock).materialize()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(NotificationOutbox).values(
                    status=DeliveryStatus.PROCESSING,
                    locked_at=locked_at,
                )
            )
            await session.execute(
                update(NotificationDelivery).values(status=DeliveryStatus.PROCESSING)
            )

    result = await DeliveryWorker(
        session_factory, FakeGateway(), clock=cast(Clock, clock)
    ).run_batch()

    assert result.sent == 1


@pytest.mark.asyncio
async def test_digest_payload_contains_safe_registration_link(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    await add_user(session_factory, 4100301, SubscriptionMode.ALL)
    linked = await add_event(session_factory, 5100301, "Linked", datetime(2030, 1, 5, tzinfo=UTC))
    unsafe = await add_event(session_factory, 5100302, "Unsafe", datetime(2030, 1, 6, tzinfo=UTC))
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Event)
                .where(Event.id == linked)
                .values(registration_url="https://share.kz/gBZ5")
            )
            await session.execute(
                update(Event)
                .where(Event.id == unsafe)
                .values(registration_url="javascript:alert(1)")
            )
    for event_id in (linked, unsafe):
        await add_change(session_factory, event_id, 1, NotificationType.NEW_EVENT)

    await outbox_service(
        session_factory, FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    ).materialize()

    async with session_factory() as session:
        outbox = await session.scalar(select(NotificationOutbox))
    assert outbox is not None
    text_value = outbox.payload["chunks"][0]["text"]
    assert "https://share.kz/gBZ5" in text_value
    assert "javascript" not in text_value


@pytest.mark.asyncio
async def test_digest_registration_link_opens_the_site_form_when_anchored(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    await add_user(session_factory, 4100311, SubscriptionMode.ALL)
    event_id = await add_event(
        session_factory, 5100311, "Anchored", datetime(2030, 1, 5, tzinfo=UTC)
    )
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Event)
                .where(Event.id == event_id)
                .values(registration_url="https://share.kz/gBZ5")
            )
            await session.execute(
                update(EventLocalization)
                .where(EventLocalization.event_id == event_id)
                .values(details_url="https://terricon.kz/ru/it#aleksanova28.10")
            )
    await add_change(session_factory, event_id, 1, NotificationType.NEW_EVENT)

    await outbox_service(
        session_factory, FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC))
    ).materialize()

    async with session_factory() as session:
        outbox = await session.scalar(select(NotificationOutbox))
    assert outbox is not None
    text_value = outbox.payload["chunks"][0]["text"]
    assert "🔗 https://terricon.kz/ru/it#aleksanova28.10" in text_value
    assert "share.kz" not in text_value


@pytest.mark.asyncio
async def test_programs_reach_only_program_subscribers_with_a_kind_badge(
    delivery_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = delivery_database
    await add_user(session_factory, 4200001, SubscriptionMode.ALL)
    category_user = await add_user(
        session_factory, 4200002, SubscriptionMode.CATEGORIES, (CategorySlug.AI_DATA,)
    )
    program_user = await add_user(session_factory, 4200003, SubscriptionMode.NONE)
    await SubscriptionService(session_factory).toggle_programs(program_user)
    both_user = await add_user(session_factory, 4200004, SubscriptionMode.ALL)
    await SubscriptionService(session_factory).toggle_programs(both_user)
    program_id = await add_event(
        session_factory,
        5200001,
        "Accelerator",
        datetime(2030, 1, 5, tzinfo=UTC),
        event_type="accelerator",
    )
    neconf_id = await add_event(
        session_factory,
        5200002,
        "Neconf",
        datetime(2030, 1, 5, tzinfo=UTC),
        event_type="neconf",
    )
    for event_id in (program_id, neconf_id):
        await add_change(
            session_factory,
            event_id,
            1,
            NotificationType.NEW_EVENT,
            new_categories=(CategorySlug.AI_DATA,),
            change_data={"new": True},
        )
    service = outbox_service(session_factory, FrozenClock(datetime(2030, 1, 1, 7, tzinfo=UTC)))

    await service.materialize()

    async with session_factory() as session:
        recipients = {
            (row.event_id, row.user_id)
            for row in (await session.scalars(select(NotificationDelivery))).all()
        }
        outbox = await session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.user_id == program_user)
        )

    assert {user for event, user in recipients if event == program_id} == {
        program_user,
        both_user,
    }
    assert category_user in {user for event, user in recipients if event == neconf_id}
    assert program_user not in {user for event, user in recipients if event == neconf_id}
    assert outbox is not None
    assert "🚀 Акселератор: Accelerator" in outbox.payload["chunks"][0]["text"]
