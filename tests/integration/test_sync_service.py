from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.sync import SyncService
from terricon_events_bot.domain.enums import (
    EventState,
    Locale,
    NotificationType,
    SourceTheme,
    SyncRunStatus,
    SyncTrigger,
)
from terricon_events_bot.domain.events import SourceEvent
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    AdminAlert,
    DomainChange,
    Event,
    EventLocalization,
    SyncRun,
    SyncState,
)
from terricon_events_bot.infrastructure.terricon.client import (
    EndpointPayload,
    TerriconRequestError,
)
from terricon_events_bot.infrastructure.terricon.dto import TerriconEventDTO
from terricon_events_bot.infrastructure.terricon.normalization import normalize_event

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "terricon"
EndpointKey = tuple[Locale, SourceTheme]


class FakeSource:
    def __init__(self, payloads: dict[EndpointKey, EndpointPayload]) -> None:
        self.payloads = payloads
        self.failures: dict[EndpointKey, TerriconRequestError] = {}

    async def fetch_detailed(self, locale: Locale, theme: SourceTheme) -> EndpointPayload:
        failure = self.failures.get((locale, theme))
        if failure is not None:
            raise failure
        return self.payloads[(locale, theme)]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2030, 1, 10, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(minutes=1)
        return current


def source_event(locale: Locale, theme: SourceTheme) -> SourceEvent:
    raw = json.loads((FIXTURES / f"{locale.value}_{theme.value}.json").read_text())[0]
    dto = TerriconEventDTO.model_validate(raw)
    event, issues = normalize_event(dto, locale, theme)
    assert not issues
    return event


def full_payloads() -> dict[EndpointKey, EndpointPayload]:
    payloads: dict[EndpointKey, EndpointPayload] = {}
    for locale in Locale:
        for theme in SourceTheme:
            event = source_event(locale, theme)
            payloads[(locale, theme)] = EndpointPayload(
                events=(event,),
                observed_source_ids=frozenset({event.source_id}),
                invalid_records=0,
                attempts=1,
            )
    return payloads


def replace_payload_events(
    source: FakeSource,
    locale: Locale,
    theme: SourceTheme,
    events: tuple[SourceEvent, ...],
    *,
    observed: frozenset[int] | None = None,
) -> None:
    source.payloads[(locale, theme)] = EndpointPayload(
        events=events,
        observed_source_ids=observed
        if observed is not None
        else frozenset(event.source_id for event in events),
        invalid_records=0,
        attempts=1,
    )


@pytest.fixture
async def sync_database(
    migrated_database_url: str,
) -> Iterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE sync_state, sync_runs, sync_endpoint_states, events, "
                "admin_alerts RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE sync_state, sync_runs, sync_endpoint_states, events, "
                    "admin_alerts RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_baseline_is_idempotent_and_creates_no_domain_changes(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    payloads = full_payloads()
    original = payloads[(Locale.RU, SourceTheme.IT)]
    payloads[(Locale.RU, SourceTheme.IT)] = replace(
        original,
        events=(original.events[0], original.events[0]),
    )
    source = FakeSource(payloads)
    service = SyncService(session_factory, source, clock=MutableClock())

    baseline = await service.run(SyncTrigger.STARTUP)
    repeated = await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(Event))
        localization_count = await session.scalar(
            select(func.count()).select_from(EventLocalization)
        )
        change_count = await session.scalar(select(func.count()).select_from(DomainChange))
        revisions = (await session.scalars(select(Event.revision))).all()
        state = await session.get(SyncState, 1)

    assert baseline.status is SyncRunStatus.SUCCEEDED
    assert baseline.created_events == 3
    assert baseline.baseline_completed
    assert repeated.updated_events == 0
    assert not repeated.baseline_completed
    assert event_count == 3
    assert localization_count == 6
    assert change_count == 0
    assert revisions == [1, 1, 1]
    assert state is not None and state.baseline_completed_at is not None


@pytest.mark.asyncio
async def test_post_baseline_new_and_important_changes_are_idempotent(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    for locale in Locale:
        original = source.payloads[(locale, SourceTheme.IT)].events[0]
        localization = replace(
            original.localization,
            title=f"{original.localization.title} second",
            content_hash=("a" if locale is Locale.RU else "b") * 64,
        )
        added = replace(original, source_id=102, localization=localization)
        replace_payload_events(
            source,
            locale,
            SourceTheme.IT,
            (original, added),
        )
    new_result = await service.run(SyncTrigger.SCHEDULED)

    for locale in Locale:
        payload = source.payloads[(locale, SourceTheme.IT)]
        original, added = payload.events
        changed_localization = replace(
            added.localization,
            title=f"{added.localization.title} changed",
            content_hash=("c" if locale is Locale.RU else "d") * 64,
        )
        changed = replace(
            added,
            starts_at=added.starts_at + timedelta(days=1),
            localization=changed_localization,
        )
        replace_payload_events(source, locale, SourceTheme.IT, (original, changed))
    changed_result = await service.run(SyncTrigger.SCHEDULED)
    repeated = await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 102))
        changes = (
            await session.scalars(
                select(DomainChange)
                .where(DomainChange.event_id == event.id)
                .order_by(DomainChange.event_revision)
            )
        ).all()

    assert new_result.created_events == 1
    assert changed_result.updated_events == 1
    assert repeated.updated_events == 0
    assert event is not None and event.revision == 2
    assert [change.notification_type for change in changes] == [
        NotificationType.NEW_EVENT,
        NotificationType.IMPORTANT_CHANGE,
    ]
    assert set(changes[1].change_data) == {"starts_at", "titles"}


@pytest.mark.asyncio
async def test_partial_failure_preserves_data_and_does_not_increment_missing_streak(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    replace_payload_events(source, Locale.RU, SourceTheme.IT, ())
    source.failures[(Locale.KZ, SourceTheme.IT)] = TerriconRequestError("timeout", 4)
    first = await service.run(SyncTrigger.SCHEDULED)
    second = await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))
        alert = await session.scalar(
            select(AdminAlert).where(
                AdminAlert.fingerprint == "sync_endpoint:kz:it",
                AdminAlert.resolved_at.is_(None),
            )
        )
        localization_count = await session.scalar(
            select(func.count())
            .select_from(EventLocalization)
            .where(EventLocalization.event_id == event.id)
        )

    assert first.status is SyncRunStatus.PARTIAL
    assert second.status is SyncRunStatus.PARTIAL
    assert event is not None and event.missing_streak == 0 and event.is_available
    assert localization_count == 2
    assert alert is not None and alert.occurrence_count == 2

    source.failures.clear()
    replace_payload_events(
        source, Locale.RU, SourceTheme.IT, (source_event(Locale.RU, SourceTheme.IT),)
    )
    await service.run(SyncTrigger.SCHEDULED)
    async with session_factory() as session:
        unresolved = await session.scalar(
            select(func.count())
            .select_from(AdminAlert)
            .where(
                AdminAlert.fingerprint == "sync_endpoint:kz:it",
                AdminAlert.resolved_at.is_(None),
            )
        )
    assert unresolved == 0


@pytest.mark.asyncio
async def test_baseline_waits_for_all_six_endpoints(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    source.failures[(Locale.KZ, SourceTheme.IT)] = TerriconRequestError("timeout", 4)
    service = SyncService(session_factory, source, clock=MutableClock())

    partial = await service.run(SyncTrigger.STARTUP)
    async with session_factory() as session:
        state = await session.get(SyncState, 1)
        changes_before = await session.scalar(select(func.count()).select_from(DomainChange))
    assert partial.status is SyncRunStatus.PARTIAL
    assert state is not None and state.baseline_completed_at is None
    assert changes_before == 0

    source.failures.clear()
    completed = await service.run(SyncTrigger.SCHEDULED)
    async with session_factory() as session:
        state = await session.get(SyncState, 1)
        changes_after = await session.scalar(select(func.count()).select_from(DomainChange))

    assert completed.baseline_completed
    assert state is not None and state.baseline_completed_at is not None
    assert changes_after == 0


@pytest.mark.asyncio
async def test_ru_common_fields_win_and_mismatch_alert_is_deduplicated(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    kz_event = source.payloads[(Locale.KZ, SourceTheme.IT)].events[0]
    replace_payload_events(
        source,
        Locale.KZ,
        SourceTheme.IT,
        (replace(kz_event, address="Different KZ address"),),
    )
    service = SyncService(session_factory, source, clock=MutableClock())

    await service.run(SyncTrigger.STARTUP)
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))
        alert = await session.scalar(
            select(AdminAlert).where(
                AdminAlert.fingerprint == "source_common_mismatch:101",
                AdminAlert.resolved_at.is_(None),
            )
        )

    assert event is not None and event.address == "Fixture Hall"
    assert alert is not None and alert.occurrence_count == 2
    assert alert.details == {"source_id": 101, "fields": ["address"]}

    replace_payload_events(
        source,
        Locale.KZ,
        SourceTheme.IT,
        (source_event(Locale.KZ, SourceTheme.IT),),
    )
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        unresolved = await session.scalar(
            select(func.count())
            .select_from(AdminAlert)
            .where(
                AdminAlert.fingerprint == "source_common_mismatch:101",
                AdminAlert.resolved_at.is_(None),
            )
        )
    assert unresolved == 0


@pytest.mark.asyncio
async def test_total_endpoint_failure_keeps_cached_events_unchanged(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)
    for key in source.payloads:
        source.failures[key] = TerriconRequestError("network", 4)

    result = await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        events = (await session.scalars(select(Event))).all()
        active_alerts = await session.scalar(
            select(func.count()).select_from(AdminAlert).where(AdminAlert.resolved_at.is_(None))
        )

    assert result.status is SyncRunStatus.FAILED
    assert len(events) == 3
    assert all(event.is_available and event.missing_streak == 0 for event in events)
    assert active_alerts == 6


@pytest.mark.asyncio
async def test_two_complete_missing_cycles_hide_event_and_recovery_restores_it(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    for locale in Locale:
        replace_payload_events(source, locale, SourceTheme.IT, ())
    first = await service.run(SyncTrigger.SCHEDULED)
    async with session_factory() as session:
        after_first = await session.scalar(select(Event).where(Event.source_id == 101))
        assert after_first is not None
        first_state = (after_first.missing_streak, after_first.is_available, after_first.revision)

    second = await service.run(SyncTrigger.SCHEDULED)
    async with session_factory() as session:
        hidden_event = await session.scalar(select(Event).where(Event.source_id == 101))
        assert hidden_event is not None
        hidden_state = (
            hidden_event.missing_streak,
            hidden_event.is_available,
            hidden_event.state,
            hidden_event.revision,
        )

    for locale in Locale:
        event = source_event(locale, SourceTheme.IT)
        replace_payload_events(source, locale, SourceTheme.IT, (event,))
    recovery = await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        restored = await session.scalar(select(Event).where(Event.source_id == 101))
        assert restored is not None
        changes = (
            await session.scalars(
                select(DomainChange)
                .where(DomainChange.event_id == restored.id)
                .order_by(DomainChange.event_revision)
            )
        ).all()

    assert first.hidden_events == 0
    assert first_state == (1, True, 1)
    assert second.hidden_events == 1
    assert hidden_state == (2, False, EventState.HIDDEN, 2)
    assert recovery.updated_events == 1
    assert restored.missing_streak == 0
    assert restored.is_available and restored.state is EventState.ACTIVE
    assert restored.revision == 3
    assert [change.notification_type for change in changes] == [
        NotificationType.CANCELLATION,
        NotificationType.IMPORTANT_CHANGE,
    ]


@pytest.mark.asyncio
async def test_invalid_but_observed_record_cannot_make_saved_event_missing(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    for locale in Locale:
        replace_payload_events(
            source,
            locale,
            SourceTheme.IT,
            (),
            observed=frozenset({101}),
        )
    await service.run(SyncTrigger.SCHEDULED)
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))

    assert event is not None and event.missing_streak == 0 and event.is_available


@pytest.mark.asyncio
async def test_hidden_events_are_not_rewritten_by_later_complete_cycles(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)
    for locale in Locale:
        replace_payload_events(source, locale, SourceTheme.IT, ())

    await service.run(SyncTrigger.SCHEDULED)
    await service.run(SyncTrigger.SCHEDULED)
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))

    assert event is not None
    assert event.missing_streak == 2
    assert event.revision == 2
    assert not event.is_available


@pytest.mark.asyncio
async def test_database_rollback_leaves_a_failed_sync_run_audit(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())

    async def fail_merge(*_args: object, **_kwargs: object) -> tuple[int, int, int]:
        raise RuntimeError("injected database transaction failure")

    monkeypatch.setattr(service, "_merge_events", fail_merge)
    with pytest.raises(RuntimeError, match="injected database transaction failure"):
        await service.run(SyncTrigger.STARTUP)

    async with session_factory() as session:
        runs = (await session.scalars(select(SyncRun))).all()
        event_count = await session.scalar(select(func.count()).select_from(Event))
        state = await session.get(SyncState, 1)

    assert len(runs) == 1
    assert runs[0].status is SyncRunStatus.FAILED
    assert runs[0].error_summary == {"database": "transaction_failed"}
    assert runs[0].finished_at is not None
    assert event_count == 0
    assert state is None
