from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.operations import (
    AdminAlertGateway,
    AdminAlertService,
    AdvisoryJobCoordinator,
    Clock,
    OperationsService,
)
from terricon_events_bot.domain.enums import (
    BroadcastAudience,
    BroadcastStatus,
    ClassificationStatus,
    DeliveryStatus,
    Locale,
    SourceTheme,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    AdminAlert,
    AppHeartbeat,
    Broadcast,
    BroadcastDelivery,
    Event,
    EventClassification,
    NotificationOutbox,
    User,
)

pytestmark = pytest.mark.postgres


class FrozenClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class FakeAlertGateway:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_alert(self, text_value: str) -> int:
        self.messages.append(text_value)
        return len(self.messages)


@pytest.fixture
async def operations_database(
    migrated_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE app_heartbeats, admin_alerts, users, events, broadcasts "
                "RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE app_heartbeats, admin_alerts, users, events, broadcasts "
                    "RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_preflight_heartbeat_and_health_use_current_schema(
    operations_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = operations_database
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    service = OperationsService(
        session_factory,
        instance_id="test-instance",
        version="test-version",
        clock=cast(Clock, clock),
    )

    await service.preflight()
    await service.heartbeat(details={"state": "test"})
    healthy = await service.health(max_heartbeat_age=timedelta(seconds=90))
    clock.current += timedelta(seconds=91)
    stale = await service.health(max_heartbeat_age=timedelta(seconds=90))

    async with session_factory() as session:
        heartbeat = await session.get(AppHeartbeat, "terricon-events-bot")
    assert heartbeat is not None
    assert heartbeat.instance_id == "test-instance"
    assert heartbeat.details == {"state": "test"}
    assert healthy.healthy
    assert not stale.healthy and stale.heartbeat_age == timedelta(seconds=91)


@pytest.mark.asyncio
async def test_advisory_job_coordinator_skips_overlapping_job(
    operations_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = operations_database
    coordinator = AdvisoryJobCoordinator(session_factory)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_operation() -> str:
        started.set()
        await release.wait()
        return "done"

    first = asyncio.create_task(coordinator.run("sync", slow_operation))
    await started.wait()
    overlapping = await coordinator.run("sync", slow_operation)
    release.set()

    assert overlapping is None
    assert await first == "done"


@pytest.mark.asyncio
async def test_admin_alerts_are_sent_once_per_observed_change(
    operations_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = operations_database
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    async with session_factory() as session:
        async with session.begin():
            session.add(
                AdminAlert(
                    kind="sync_endpoint_failed",
                    fingerprint="sync_endpoint:kz:it",
                    details={},
                    occurrence_count=1,
                    first_seen_at=clock.current,
                    last_seen_at=clock.current,
                )
            )
    gateway = FakeAlertGateway()
    service = AdminAlertService(
        session_factory,
        cast(AdminAlertGateway, gateway),
        clock=cast(Clock, clock),
    )

    assert await service.dispatch() == 1
    assert await service.dispatch() == 0
    clock.current += timedelta(minutes=1)
    async with session_factory() as session:
        async with session.begin():
            alert = await session.scalar(select(AdminAlert))
            assert alert is not None
            alert.occurrence_count += 1
            alert.last_seen_at = clock.current
    assert await service.dispatch() == 1
    assert len(gateway.messages) == 2


@pytest.mark.asyncio
async def test_operational_alerts_group_prolonged_queue_failures_and_resolve(
    operations_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = operations_database
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    async with session_factory() as session:
        async with session.begin():
            user = User(
                telegram_id=7_500_001,
                locale=Locale.RU,
                is_active=True,
                access_granted=True,
            )
            event = Event(
                source_id=7_500_100,
                source_theme=SourceTheme.IT,
                starts_at=clock.current,
                semantic_hash="a" * 64,
            )
            broadcast = Broadcast(
                created_by_telegram_id=9_500_001,
                audience=BroadcastAudience.RU,
                status=BroadcastStatus.RUNNING,
            )
            session.add_all([user, event, broadcast])
            await session.flush()
            session.add_all(
                [
                    EventClassification(
                        event_id=event.id,
                        status=ClassificationStatus.FALLBACK,
                        semantic_hash=event.semantic_hash,
                        last_error_code="openai_timeout",
                        result_metadata={},
                    ),
                    NotificationOutbox(
                        user_id=user.id,
                        batch_id=uuid4(),
                        batch_key="audit-outbox",
                        status=DeliveryStatus.RETRY_WAIT,
                        payload={"version": 1, "chunks": []},
                        attempts=5,
                        available_at=clock.current,
                        created_at=clock.current - timedelta(hours=2),
                        last_error_code="telegram_network",
                    ),
                    BroadcastDelivery(
                        broadcast_id=broadcast.id,
                        user_id=user.id,
                        status=DeliveryStatus.FAILED,
                        attempts=1,
                        last_error_code="telegram_bad_request",
                    ),
                ]
            )
    gateway = FakeAlertGateway()
    service = AdminAlertService(
        session_factory,
        cast(AdminAlertGateway, gateway),
        clock=cast(Clock, clock),
    )

    assert await service.refresh_operational() == 3
    assert await service.dispatch() == 3
    assert await service.refresh_operational() == 3
    assert await service.dispatch() == 0
    assert any("Причина: openai_timeout" in message for message in gateway.messages)
    assert any("Причина: telegram_network" in message for message in gateway.messages)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(EventClassification).values(
                    status=ClassificationStatus.COMPLETED,
                    last_error_code=None,
                )
            )
            await session.execute(
                update(NotificationOutbox).values(
                    status=DeliveryStatus.SENT,
                    last_error_code=None,
                )
            )
            await session.execute(
                update(BroadcastDelivery).values(
                    status=DeliveryStatus.SENT,
                    last_error_code=None,
                )
            )
    assert await service.refresh_operational() == 0
    async with session_factory() as session:
        unresolved = await session.scalar(
            select(AdminAlert.id).where(AdminAlert.resolved_at.is_(None))
        )
    assert unresolved is None
