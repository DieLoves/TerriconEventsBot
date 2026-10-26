from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.sync import SyncService
from terricon_events_bot.domain.enums import (
    CategorySlug,
    ClassificationStatus,
    EventState,
    Locale,
    NotificationType,
    SourceTheme,
    SyncRunStatus,
    SyncSource,
    SyncTrigger,
    TranslationSource,
)
from terricon_events_bot.domain.events import SourceEvent
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    AdminAlert,
    DomainChange,
    Event,
    EventCategory,
    EventClassification,
    EventLocalization,
    SyncEndpointState,
    SyncRun,
    SyncRunEndpoint,
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
    """Theme endpoints from ``payloads``; with ``stage`` eventsByStage is their union.

    Without ``stage`` the primary source is unavailable, so every cycle is a fallback.
    """

    def __init__(
        self, payloads: dict[EndpointKey, EndpointPayload], *, stage: bool = False
    ) -> None:
        self.payloads = payloads
        self.failures: dict[EndpointKey, TerriconRequestError] = {}
        self.stage = stage
        self.stage_failures: dict[Locale, TerriconRequestError] = {}
        self.stage_only: dict[Locale, tuple[SourceEvent, ...]] = {}
        self.stage_calls = 0
        self.legacy_calls = 0

    async def fetch_stage(self, locale: Locale) -> EndpointPayload:
        self.stage_calls += 1
        if not self.stage:
            raise TerriconRequestError("stage_disabled", 0)
        failure = self.stage_failures.get(locale)
        if failure is not None:
            raise failure
        themed = [self.payloads[(locale, theme)] for theme in SourceTheme]
        extra = self.stage_only.get(locale, ())
        return EndpointPayload(
            events=tuple(event for payload in themed for event in payload.events) + extra,
            observed_source_ids=frozenset(
                source_id for payload in themed for source_id in payload.observed_source_ids
            )
            | {event.source_id for event in extra},
            invalid_records=sum(payload.invalid_records for payload in themed),
            attempts=1,
        )

    async def fetch_detailed(self, locale: Locale, theme: SourceTheme) -> EndpointPayload:
        self.legacy_calls += 1
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
        classifications = (await session.scalars(select(EventClassification))).all()
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
    assert len(classifications) == 3
    assert all(item.status is ClassificationStatus.PENDING for item in classifications)
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
        # Upcoming relative to MutableClock: past new events are stored silently.
        added = replace(
            original,
            source_id=102,
            starts_at=datetime(2030, 2, 1, 10, tzinfo=UTC),
            localization=localization,
        )
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
    assert changes == []
    async with session_factory() as session:
        classification = await session.get(EventClassification, event.id)
    assert classification is not None
    assert classification.status is ClassificationStatus.PENDING
    assert classification.result_metadata["notify_new"] is True


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
    source = FakeSource(full_payloads(), stage=True)
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
    source = FakeSource(full_payloads(), stage=True)
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
    source = FakeSource(full_payloads(), stage=True)
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
    source = FakeSource(full_payloads(), stage=True)
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


@pytest.mark.asyncio
async def test_official_localization_replaces_machine_without_spurious_reclassification(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    async with session_factory() as session:
        async with session.begin():
            event = await session.scalar(select(Event).where(Event.source_id == 101))
            assert event is not None
            classification = await session.get(EventClassification, event.id)
            localization = await session.get(EventLocalization, (event.id, Locale.KZ))
            assert classification is not None and localization is not None
            classification.status = ClassificationStatus.COMPLETED
            classification.next_attempt_at = None
            localization.title = "Machine title"
            localization.translation_source = TranslationSource.MACHINE

    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))
        assert event is not None
        classification = await session.get(EventClassification, event.id)
        localization = await session.get(EventLocalization, (event.id, Locale.KZ))

    assert classification is not None
    assert classification.status is ClassificationStatus.COMPLETED
    assert localization is not None
    assert localization.translation_source is TranslationSource.OFFICIAL
    assert localization.title == source_event(Locale.KZ, SourceTheme.IT).localization.title


@pytest.mark.asyncio
async def test_only_semantic_changes_requeue_completed_classification(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    async with session_factory() as session:
        async with session.begin():
            event = await session.scalar(select(Event).where(Event.source_id == 101))
            assert event is not None
            classification = await session.get(EventClassification, event.id)
            assert classification is not None
            classification.status = ClassificationStatus.COMPLETED
            classification.classified_at = datetime(2030, 1, 10, tzinfo=UTC)
            classification.next_attempt_at = None
            original_hash = classification.semantic_hash

    for locale in Locale:
        original = source.payloads[(locale, SourceTheme.IT)].events[0]
        replace_payload_events(
            source,
            locale,
            SourceTheme.IT,
            (replace(original, starts_at=original.starts_at + timedelta(days=1)),),
        )
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))
        assert event is not None
        classification = await session.get(EventClassification, event.id)
    assert classification is not None
    assert classification.status is ClassificationStatus.COMPLETED
    assert classification.semantic_hash == original_hash

    for locale in Locale:
        dated = source.payloads[(locale, SourceTheme.IT)].events[0]
        localization = replace(
            dated.localization,
            title=f"{dated.localization.title} changed",
            content_hash=("e" if locale is Locale.RU else "f") * 64,
        )
        replace_payload_events(
            source,
            locale,
            SourceTheme.IT,
            (replace(dated, localization=localization),),
        )
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        classification = await session.get(EventClassification, event.id)
    assert classification is not None
    assert classification.status is ClassificationStatus.PENDING
    assert classification.semantic_hash != original_hash
    assert classification.result_metadata["notify_new"] is False


@pytest.mark.asyncio
async def test_semantic_change_removes_stale_machine_localization(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    payloads = full_payloads()
    source = FakeSource(payloads)
    replace_payload_events(source, Locale.KZ, SourceTheme.IT, ())
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    async with session_factory() as session:
        async with session.begin():
            event = await session.scalar(select(Event).where(Event.source_id == 101))
            assert event is not None
            classification = await session.get(EventClassification, event.id)
            assert classification is not None
            classification.status = ClassificationStatus.COMPLETED
            classification.next_attempt_at = None
            session.add(
                EventLocalization(
                    event_id=event.id,
                    locale=Locale.KZ,
                    title="Old machine title",
                    description="Old machine description",
                    translation_source=TranslationSource.MACHINE,
                    content_hash="f" * 64,
                )
            )

    unchanged = await service.run(SyncTrigger.SCHEDULED)
    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))
        assert event is not None
        unchanged_revision = event.revision
        change_count = await session.scalar(
            select(func.count()).select_from(DomainChange).where(DomainChange.event_id == event.id)
        )

    assert unchanged.updated_events == 0
    assert unchanged_revision == 1
    assert change_count == 0

    original = source.payloads[(Locale.RU, SourceTheme.IT)].events[0]
    changed_localization = replace(
        original.localization,
        title=f"{original.localization.title} changed",
        content_hash="e" * 64,
    )
    replace_payload_events(
        source,
        Locale.RU,
        SourceTheme.IT,
        (replace(original, localization=changed_localization),),
    )
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.source_id == 101))
        assert event is not None
        classification = await session.get(EventClassification, event.id)
        machine = await session.get(EventLocalization, (event.id, Locale.KZ))

    assert machine is None
    assert classification is not None
    assert classification.status is ClassificationStatus.PENDING
    assert classification.result_metadata["queued_revision"] == event.revision


@pytest.mark.asyncio
async def test_unannounced_event_suppresses_changes_until_active_again(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads(), stage=True)
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    added_by_locale: dict[Locale, SourceEvent] = {}
    for locale in Locale:
        original = source.payloads[(locale, SourceTheme.IT)].events[0]
        added = replace(
            original,
            source_id=103,
            starts_at=datetime(2030, 2, 1, 10, tzinfo=UTC),
            localization=replace(
                original.localization,
                title=f"{original.localization.title} new",
                content_hash=("1" if locale is Locale.RU else "2") * 64,
            ),
        )
        added_by_locale[locale] = added
        replace_payload_events(source, locale, SourceTheme.IT, (original, added))
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        async with session.begin():
            event = await session.scalar(select(Event).where(Event.source_id == 103))
            assert event is not None
            classification = await session.get(EventClassification, event.id)
            assert classification is not None
            classification.status = ClassificationStatus.COMPLETED
            classification.classified_at = datetime(2030, 1, 10, tzinfo=UTC)
            classification.next_attempt_at = None
            session.add(EventCategory(event_id=event.id, category=CategorySlug.DEVELOPMENT_IT))

    for locale in Locale:
        original = source.payloads[(locale, SourceTheme.IT)].events[0]
        replace_payload_events(source, locale, SourceTheme.IT, (original,))
    await service.run(SyncTrigger.SCHEDULED)
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        hidden = await session.scalar(select(Event).where(Event.source_id == 103))
        assert hidden is not None
        hidden_changes = (
            await session.scalars(select(DomainChange).where(DomainChange.event_id == hidden.id))
        ).all()
    assert not hidden.is_available
    assert hidden_changes == []

    for locale in Locale:
        original = source.payloads[(locale, SourceTheme.IT)].events[0]
        replace_payload_events(source, locale, SourceTheme.IT, (original, added_by_locale[locale]))
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        restored = await session.scalar(select(Event).where(Event.source_id == 103))
        assert restored is not None
        classification = await session.get(EventClassification, restored.id)
        changes = (
            await session.scalars(select(DomainChange).where(DomainChange.event_id == restored.id))
        ).all()

    assert restored.is_available
    assert classification is not None
    assert classification.result_metadata["notify_new"] is False
    assert [change.notification_type for change in changes] == [NotificationType.NEW_EVENT]
    assert changes[0].new_categories == ["development_it"]


def stage_record(locale: Locale, source_id: int) -> SourceEvent:
    payload = json.loads((FIXTURES / f"events_by_stage_{locale.value}.json").read_text())
    raw = next(
        record
        for record in (*payload["eventsNew"], *payload["eventsPreparation"])
        if record["id"] == source_id
    )
    event, issues = normalize_event(TerriconEventDTO.model_validate(raw), locale, None)
    assert not issues
    return event


@pytest.mark.asyncio
async def test_stage_cycle_is_primary_and_audited_per_locale(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads(), stage=True)
    source.stage_only = {locale: (stage_record(locale, 902),) for locale in Locale}
    service = SyncService(session_factory, source, clock=MutableClock())

    result = await service.run(SyncTrigger.STARTUP)

    async with session_factory() as session:
        run = (await session.scalars(select(SyncRun))).one()
        endpoints = (await session.scalars(select(SyncRunEndpoint))).all()
        states = (await session.scalars(select(SyncEndpointState))).all()
        program = await session.scalar(select(Event).where(Event.source_id == 902))

    assert result.status is SyncRunStatus.SUCCEEDED and result.baseline_completed
    assert source.legacy_calls == 0
    assert run.source is SyncSource.STAGE
    assert {(row.source, row.locale, row.source_theme) for row in endpoints} == {
        (SyncSource.STAGE, Locale.RU, None),
        (SyncSource.STAGE, Locale.KZ, None),
    }
    assert len(states) == 2
    assert program is not None and program.event_type == "accelerator"
    assert program.source_theme is SourceTheme.BUSINESS


@pytest.mark.asyncio
async def test_fallback_keeps_programs_and_alerts_only_when_prolonged(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads(), stage=True)
    source.stage_only = {locale: (stage_record(locale, 902),) for locale in Locale}
    service = SyncService(
        session_factory,
        source,
        clock=MutableClock(),
        fallback_alert_after=timedelta(minutes=2),
    )
    await service.run(SyncTrigger.STARTUP)

    source.stage_failures[Locale.KZ] = TerriconRequestError("timeout", 4)
    fallback_results = [await service.run(SyncTrigger.SCHEDULED) for _ in range(3)]

    async with session_factory() as session:
        program = await session.scalar(select(Event).where(Event.source_id == 902))
        assert program is not None
        changes = (await session.scalars(select(DomainChange))).all()
        state = await session.get(SyncState, 1)
        alerts = {
            alert.fingerprint: alert for alert in (await session.scalars(select(AdminAlert))).all()
        }
        sources = (await session.scalars(select(SyncRun.source).order_by(SyncRun.id))).all()

    assert all(result.status is SyncRunStatus.SUCCEEDED for result in fallback_results)
    assert sources == [SyncSource.STAGE, SyncSource.LEGACY, SyncSource.LEGACY, SyncSource.LEGACY]
    assert program.is_available and program.missing_streak == 0
    assert changes == []
    assert state is not None and state.fallback_since is not None
    assert not any(fingerprint.startswith("sync_endpoint:") for fingerprint in alerts)
    assert alerts["sync_source_fallback"].resolved_at is None

    source.stage_failures.clear()
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        state = await session.get(SyncState, 1)
        alert = await session.scalar(
            select(AdminAlert).where(AdminAlert.fingerprint == "sync_source_fallback")
        )
    assert state is not None and state.fallback_since is None
    assert alert is not None and alert.resolved_at is not None


@pytest.mark.asyncio
async def test_short_fallback_does_not_alert_and_counts_no_absence(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads(), stage=True)
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    source.stage_failures[Locale.RU] = TerriconRequestError("http_500", 4)
    for locale in Locale:
        replace_payload_events(source, locale, SourceTheme.MARKETING, ())
    await service.run(SyncTrigger.SCHEDULED)
    await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        marketing = (
            await session.scalars(select(Event).where(Event.source_theme == SourceTheme.MARKETING))
        ).all()
        alert_count = await session.scalar(select(func.count()).select_from(AdminAlert))

    # getAllEvents is incomplete by design: nothing is hidden while it is used.
    assert marketing and all(
        event.is_available and event.missing_streak == 0 for event in marketing
    )
    assert alert_count == 0


@pytest.mark.asyncio
async def test_new_source_records_notify_only_while_upcoming(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads(), stage=True)
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)

    # Clock is 2030-01-10: neconf 901 is on 2030-01-20, partner conference 904 is past.
    source.stage_only = {
        locale: (stage_record(locale, 901), stage_record(locale, 904)) for locale in Locale
    }
    result = await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Event.source_id, EventClassification.result_metadata)
                .join(EventClassification, EventClassification.event_id == Event.id)
                .where(Event.source_id.in_((901, 904)))
            )
        ).all()
        change_count = await session.scalar(select(func.count()).select_from(DomainChange))

    assert result.created_events == 2
    assert {source_id: metadata["notify_new"] for source_id, metadata in rows} == {
        901: True,
        904: False,
    }
    assert change_count == 0


@pytest.mark.asyncio
async def test_filling_event_type_of_existing_events_is_not_a_change(
    sync_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = sync_database
    source = FakeSource(full_payloads(), stage=True)
    service = SyncService(session_factory, source, clock=MutableClock())
    await service.run(SyncTrigger.STARTUP)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(update(Event).values(event_type=None))

    result = await service.run(SyncTrigger.SCHEDULED)

    async with session_factory() as session:
        events = (await session.scalars(select(Event))).all()
    assert result.updated_events == 0
    assert {event.event_type for event in events} == {"lecture", "edu", "mitup"}
    assert all(event.revision == 1 for event in events)
