from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.domain.enums import (
    ClassificationStatus,
    EventState,
    Locale,
    NotificationType,
    SourceTheme,
    SyncEndpointStatus,
    SyncRunStatus,
    SyncTrigger,
    TranslationSource,
)
from terricon_events_bot.domain.event_changes import (
    canonical_hash,
    important_hash,
    semantic_hash,
    structured_diff,
)
from terricon_events_bot.domain.events import SourceEvent, SourceEventLocalization
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

Clock = Callable[[], datetime]
EndpointKey = tuple[Locale, SourceTheme]

_LOCALE_PRIORITY = {Locale.RU: 0, Locale.KZ: 1}
_THEME_PRIORITY = {theme: index for index, theme in enumerate(SourceTheme)}
_COMMON_FIELDS = (
    "starts_at",
    "event_format",
    "event_languages",
    "address",
    "registration_url",
    "recording_url",
    "poster_url",
)


class TerriconSource(Protocol):
    async def fetch_detailed(self, locale: Locale, theme: SourceTheme) -> EndpointPayload: ...


@dataclass(frozen=True, slots=True)
class SyncResult:
    status: SyncRunStatus
    successful_endpoints: int
    failed_endpoints: int
    records_seen: int
    created_events: int
    updated_events: int
    hidden_events: int
    baseline_completed: bool


@dataclass(frozen=True, slots=True)
class _EndpointOutcome:
    locale: Locale
    theme: SourceTheme
    payload: EndpointPayload | None
    error_code: str | None
    attempts: int

    @property
    def succeeded(self) -> bool:
        return self.payload is not None


@dataclass(frozen=True, slots=True)
class _MergedEvent:
    common: SourceEvent
    localizations: Mapping[Locale, SourceEventLocalization]


@dataclass(frozen=True, slots=True)
class _AlertData:
    kind: str
    details: dict[str, object]


class SyncService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source: TerriconSource,
        *,
        clock: Clock = lambda: datetime.now(UTC),
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    async def run(self, trigger: SyncTrigger) -> SyncResult:
        outcomes = await asyncio.gather(
            *(self._fetch_endpoint(locale, theme) for locale in Locale for theme in SourceTheme)
        )
        now = self._normalized_now()
        sync_run_id = await self._create_run_audit(trigger, outcomes, now)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await self._persist_cycle(session, sync_run_id, outcomes, now)
            return result
        except Exception as error:
            self._logger.exception(
                "Sync database transaction failed",
                extra={"error_code": type(error).__name__, "sync_run_id": sync_run_id},
            )
            try:
                await self._mark_run_failed(sync_run_id, now)
            except Exception:
                self._logger.exception(
                    "Could not persist sync database failure audit",
                    extra={"error_code": "audit_persist_failed", "sync_run_id": sync_run_id},
                )
            raise

    async def _create_run_audit(
        self,
        trigger: SyncTrigger,
        outcomes: list[_EndpointOutcome],
        now: datetime,
    ) -> int:
        successful = [outcome for outcome in outcomes if outcome.succeeded]
        failed = [outcome for outcome in outcomes if not outcome.succeeded]
        records_seen = sum(
            len(outcome.payload.events) + outcome.payload.invalid_records
            for outcome in successful
            if outcome.payload is not None
        )
        async with self._session_factory() as session:
            async with session.begin():
                baseline_completed_at = await session.scalar(
                    select(SyncState.baseline_completed_at).where(SyncState.id == 1)
                )
                sync_run = SyncRun(
                    trigger=trigger,
                    status=SyncRunStatus.RUNNING,
                    is_baseline=baseline_completed_at is None,
                    successful_endpoints=len(successful),
                    failed_endpoints=len(failed),
                    records_seen=records_seen,
                    error_summary={
                        f"{outcome.locale.value}:{outcome.theme.value}": outcome.error_code
                        for outcome in failed
                    },
                    started_at=now,
                )
                session.add(sync_run)
                await session.flush()
                return sync_run.id

    async def _mark_run_failed(self, sync_run_id: int, now: datetime) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                sync_run = await session.get(SyncRun, sync_run_id, with_for_update=True)
                if sync_run is None:
                    return
                sync_run.status = SyncRunStatus.FAILED
                sync_run.finished_at = now
                sync_run.error_summary = {
                    **sync_run.error_summary,
                    "database": "transaction_failed",
                }

    async def _fetch_endpoint(self, locale: Locale, theme: SourceTheme) -> _EndpointOutcome:
        try:
            payload = await self._source.fetch_detailed(locale, theme)
        except TerriconRequestError as error:
            self._logger.warning(
                "Terricon endpoint failed after retries",
                extra={
                    "attempts": error.attempts,
                    "error_code": error.code,
                    "locale": locale.value,
                    "source_theme": theme.value,
                },
            )
            return _EndpointOutcome(locale, theme, None, error.code, error.attempts)
        except Exception as error:
            self._logger.exception(
                "Unexpected Terricon endpoint failure",
                extra={
                    "error_code": type(error).__name__,
                    "locale": locale.value,
                    "source_theme": theme.value,
                },
            )
            return _EndpointOutcome(locale, theme, None, "unexpected_error", 1)
        return _EndpointOutcome(locale, theme, payload, None, payload.attempts)

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("SyncService clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    async def _persist_cycle(
        self,
        session: AsyncSession,
        sync_run_id: int,
        outcomes: list[_EndpointOutcome],
        now: datetime,
    ) -> SyncResult:
        successful = [outcome for outcome in outcomes if outcome.succeeded]
        failed = [outcome for outcome in outcomes if not outcome.succeeded]
        status = (
            SyncRunStatus.SUCCEEDED
            if len(successful) == len(Locale) * len(SourceTheme)
            else SyncRunStatus.PARTIAL
            if successful
            else SyncRunStatus.FAILED
        )
        records_seen = sum(
            len(outcome.payload.events) + outcome.payload.invalid_records
            for outcome in successful
            if outcome.payload is not None
        )

        sync_state = await session.get(SyncState, 1, with_for_update=True)
        if sync_state is None:
            sync_state = SyncState(id=1)
            session.add(sync_state)
            await session.flush()
        baseline_before = sync_state.baseline_completed_at is not None
        sync_run = await session.get(SyncRun, sync_run_id, with_for_update=True)
        if sync_run is None:
            raise RuntimeError("Sync run audit row disappeared")
        sync_run.status = status
        sync_run.is_baseline = not baseline_before
        sync_run.finished_at = now

        alerts: dict[str, _AlertData] = {}
        resolved_alerts: set[str] = set()
        await self._update_endpoint_tracking(
            session, sync_run.id, outcomes, now, alerts, resolved_alerts
        )

        merged, merge_alerts = self._merge_successful_payloads(successful)
        alerts.update(merge_alerts)
        full_success = status is SyncRunStatus.SUCCEEDED
        if full_success:
            resolved_alerts.update(
                (
                    await session.scalars(
                        select(AdminAlert.fingerprint).where(
                            AdminAlert.kind.in_(
                                ("source_common_mismatch", "source_theme_collision")
                            ),
                            AdminAlert.resolved_at.is_(None),
                        )
                    )
                ).all()
            )
        complete_themes = {
            theme
            for theme in SourceTheme
            if all(
                any(
                    outcome.succeeded and outcome.locale is locale and outcome.theme is theme
                    for outcome in outcomes
                )
                for locale in Locale
            )
        }
        observed_by_theme: dict[SourceTheme, set[int]] = defaultdict(set)
        observed_all: set[int] = set()
        for outcome in successful:
            assert outcome.payload is not None
            observed_by_theme[outcome.theme].update(outcome.payload.observed_source_ids)
            observed_all.update(outcome.payload.observed_source_ids)

        created, updated_events, hidden = await self._merge_events(
            session,
            merged,
            observed_all,
            observed_by_theme,
            complete_themes,
            baseline_before,
            now,
        )

        baseline_completed = full_success and not baseline_before
        sync_state.last_attempt_at = now
        if full_success:
            sync_state.last_full_success_at = now
            if sync_state.baseline_completed_at is None:
                sync_state.baseline_completed_at = now

        resolved_alerts.difference_update(alerts)
        await self._apply_alerts(session, alerts, resolved_alerts, now)
        return SyncResult(
            status=status,
            successful_endpoints=len(successful),
            failed_endpoints=len(failed),
            records_seen=records_seen,
            created_events=created,
            updated_events=updated_events,
            hidden_events=hidden,
            baseline_completed=baseline_completed,
        )

    async def _update_endpoint_tracking(
        self,
        session: AsyncSession,
        sync_run_id: int,
        outcomes: list[_EndpointOutcome],
        now: datetime,
        alerts: dict[str, _AlertData],
        resolved_alerts: set[str],
    ) -> None:
        states = {
            (state.locale, state.source_theme): state
            for state in (await session.scalars(select(SyncEndpointState))).all()
        }
        for outcome in outcomes:
            key = (outcome.locale, outcome.theme)
            state = states.get(key)
            if state is None:
                state = SyncEndpointState(
                    locale=outcome.locale,
                    source_theme=outcome.theme,
                    consecutive_failures=0,
                )
                session.add(state)
                states[key] = state
            state.last_attempt_at = now
            fingerprint = f"sync_endpoint:{outcome.locale.value}:{outcome.theme.value}"
            if outcome.succeeded:
                assert outcome.payload is not None
                state.last_status = SyncEndpointStatus.SUCCEEDED
                state.consecutive_failures = 0
                state.last_success_at = now
                state.last_error_code = None
                endpoint_status = SyncEndpointStatus.SUCCEEDED
                records_seen = len(outcome.payload.events) + outcome.payload.invalid_records
                resolved_alerts.add(fingerprint)
            else:
                state.last_status = SyncEndpointStatus.FAILED
                state.consecutive_failures += 1
                state.last_error_code = outcome.error_code
                endpoint_status = SyncEndpointStatus.FAILED
                records_seen = 0
                alerts[fingerprint] = _AlertData(
                    kind="sync_endpoint_failure",
                    details={
                        "error_code": outcome.error_code,
                        "locale": outcome.locale.value,
                        "source_theme": outcome.theme.value,
                    },
                )
            session.add(
                SyncRunEndpoint(
                    sync_run_id=sync_run_id,
                    locale=outcome.locale,
                    source_theme=outcome.theme,
                    status=endpoint_status,
                    attempts=outcome.attempts,
                    records_seen=records_seen,
                    error_code=outcome.error_code,
                    error_details={},
                    started_at=now,
                    finished_at=now,
                )
            )

    def _merge_successful_payloads(
        self, outcomes: list[_EndpointOutcome]
    ) -> tuple[dict[int, _MergedEvent], dict[str, _AlertData]]:
        grouped: dict[int, list[SourceEvent]] = defaultdict(list)
        for outcome in sorted(
            outcomes,
            key=lambda value: (_THEME_PRIORITY[value.theme], _LOCALE_PRIORITY[value.locale]),
        ):
            assert outcome.payload is not None
            grouped_events: set[tuple[int, Locale]] = set()
            for event in outcome.payload.events:
                duplicate_key = (event.source_id, event.localization.locale)
                if duplicate_key in grouped_events:
                    continue
                grouped_events.add(duplicate_key)
                grouped[event.source_id].append(event)

        merged: dict[int, _MergedEvent] = {}
        alerts: dict[str, _AlertData] = {}
        for source_id, candidates in grouped.items():
            themes = {candidate.source_theme for candidate in candidates}
            chosen_theme = min(themes, key=_THEME_PRIORITY.__getitem__)
            if len(themes) > 1:
                alerts[f"source_theme_collision:{source_id}"] = _AlertData(
                    kind="source_theme_collision",
                    details={
                        "source_id": source_id,
                        "themes": sorted(theme.value for theme in themes),
                    },
                )
            same_theme = [item for item in candidates if item.source_theme is chosen_theme]
            same_theme.sort(key=lambda item: _LOCALE_PRIORITY[item.localization.locale])
            common = same_theme[0]
            mismatches = [
                field
                for field in _COMMON_FIELDS
                if any(
                    getattr(candidate, field) != getattr(common, field)
                    for candidate in same_theme[1:]
                )
            ]
            if mismatches:
                alerts[f"source_common_mismatch:{source_id}"] = _AlertData(
                    kind="source_common_mismatch",
                    details={"source_id": source_id, "fields": sorted(mismatches)},
                )
            localizations = {
                candidate.localization.locale: candidate.localization for candidate in same_theme
            }
            merged[source_id] = _MergedEvent(common=common, localizations=localizations)
        return merged, alerts

    async def _merge_events(
        self,
        session: AsyncSession,
        merged: Mapping[int, _MergedEvent],
        observed_all: set[int],
        observed_by_theme: Mapping[SourceTheme, set[int]],
        complete_themes: set[SourceTheme],
        baseline_before: bool,
        now: datetime,
    ) -> tuple[int, int, int]:
        relevant_ids = set(merged) | observed_all
        db_events: list[Event] = []
        if relevant_ids:
            db_events.extend(
                (
                    await session.scalars(
                        select(Event).where(Event.source_id.in_(relevant_ids)).with_for_update()
                    )
                ).all()
            )
        if complete_themes:
            missing_candidates = select(Event).where(
                Event.source_theme.in_(complete_themes),
                Event.is_available.is_(True),
            )
            if relevant_ids:
                missing_candidates = missing_candidates.where(Event.source_id.not_in(relevant_ids))
            db_events.extend((await session.scalars(missing_candidates.with_for_update())).all())
        events_by_source = {event.source_id: event for event in db_events}
        new_source_ids: set[int] = set()
        for source_id, incoming in merged.items():
            if source_id in events_by_source:
                continue
            common = incoming.common
            event = Event(
                source_id=source_id,
                source_theme=common.source_theme,
                starts_at=common.starts_at,
                event_format=common.event_format,
                event_languages=list(common.event_languages),
                address=common.address,
                registration_url=common.registration_url,
                recording_url=common.recording_url,
                poster_url=common.poster_url,
                state=EventState.ACTIVE,
                is_available=True,
                missing_streak=0,
                revision=1,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(event)
            events_by_source[source_id] = event
            db_events.append(event)
            new_source_ids.add(source_id)
        await session.flush()

        event_ids = [event.id for event in db_events]
        localization_rows = (
            (
                await session.scalars(
                    select(EventLocalization).where(EventLocalization.event_id.in_(event_ids))
                )
            ).all()
            if event_ids
            else []
        )
        localizations: dict[int, dict[Locale, EventLocalization]] = defaultdict(dict)
        for row in localization_rows:
            localizations[row.event_id][row.locale] = row
        categories: dict[int, list[str]] = defaultdict(list)
        classifications: dict[int, EventClassification] = {}
        if event_ids:
            category_rows = (
                await session.execute(
                    select(EventCategory.event_id, EventCategory.category).where(
                        EventCategory.event_id.in_(event_ids)
                    )
                )
            ).all()
            for event_id, category in category_rows:
                categories[event_id].append(category.value)
            classifications = {
                classification.event_id: classification
                for classification in (
                    await session.scalars(
                        select(EventClassification).where(
                            EventClassification.event_id.in_(event_ids)
                        )
                    )
                ).all()
            }

        created = len(new_source_ids)
        updated_events = 0
        hidden = 0
        for source_id, incoming in merged.items():
            event = events_by_source[source_id]
            is_new = source_id in new_source_ids
            old_localization_data = self._official_localization_data(localizations[event.id])
            old_important = self._important_snapshot(event, old_localization_data)
            material_changed = False

            for field in _COMMON_FIELDS:
                new_value: object = getattr(incoming.common, field)
                if field == "event_languages":
                    new_value = list(incoming.common.event_languages)
                if getattr(event, field) != new_value:
                    material_changed = True
                    setattr(event, field, new_value)
            if event.source_theme is not incoming.common.source_theme:
                material_changed = True
                event.source_theme = incoming.common.source_theme
            if not event.is_available or event.state is EventState.HIDDEN:
                material_changed = True
                event.is_available = True
                event.state = EventState.ACTIVE
            event.missing_streak = 0
            event.last_seen_at = now

            for locale, source_localization in incoming.localizations.items():
                localization = localizations[event.id].get(locale)
                if localization is None:
                    localization = EventLocalization(
                        event_id=event.id,
                        locale=locale,
                        title=source_localization.title,
                        description=source_localization.description,
                        audience=source_localization.audience,
                        speaker=source_localization.speaker,
                        details_url=source_localization.details_url,
                        translation_source=TranslationSource.OFFICIAL,
                        content_hash=source_localization.content_hash,
                    )
                    session.add(localization)
                    localizations[event.id][locale] = localization
                    material_changed = True
                else:
                    if (
                        localization.content_hash != source_localization.content_hash
                        or localization.translation_source is not TranslationSource.OFFICIAL
                    ):
                        material_changed = True
                    localization.title = source_localization.title
                    localization.description = source_localization.description
                    localization.audience = source_localization.audience
                    localization.speaker = source_localization.speaker
                    localization.details_url = source_localization.details_url
                    localization.translation_source = TranslationSource.OFFICIAL
                    localization.content_hash = source_localization.content_hash

            new_semantic_hash = semantic_hash(
                self._official_localization_data(localizations[event.id])
            )
            semantic_changed = event.semantic_hash != new_semantic_hash
            if semantic_changed:
                for locale, localization in list(localizations[event.id].items()):
                    if (
                        locale not in incoming.localizations
                        and localization.translation_source is TranslationSource.MACHINE
                    ):
                        await session.delete(localization)
                        del localizations[event.id][locale]

            official_rows = [
                localization
                for localization in localizations[event.id].values()
                if localization.translation_source is TranslationSource.OFFICIAL
            ]
            if len(official_rows) == 1:
                source_details_url = official_rows[0].details_url
                for localization in localizations[event.id].values():
                    if localization.translation_source is TranslationSource.MACHINE:
                        localization.details_url = source_details_url
                        localization.content_hash = canonical_hash(
                            {
                                "audience": localization.audience,
                                "description": localization.description,
                                "details_url": localization.details_url,
                                "speaker": localization.speaker,
                                "title": localization.title,
                            }
                        )

            new_localization_data = self._official_localization_data(localizations[event.id])
            new_important = self._important_snapshot(event, new_localization_data)
            new_important_hash = important_hash(new_important)
            if (
                event.semantic_hash != new_semantic_hash
                or event.important_hash != new_important_hash
            ):
                material_changed = True
            event.semantic_hash = new_semantic_hash
            event.important_hash = new_important_hash

            classification = classifications.get(event.id)
            if not is_new and material_changed:
                event.revision += 1
                updated_events += 1
            if classification is None or semantic_changed:
                classification = self._queue_classification(
                    session,
                    event,
                    classification,
                    semantic_hash_value=new_semantic_hash,
                    notify_new=is_new and baseline_before,
                    now=now,
                )
                classifications[event.id] = classification

            released_new = False
            if classification is not None:
                released_new = await self._release_ready_new_event(
                    session,
                    event,
                    classification,
                    categories[event.id],
                    now,
                )
            if is_new:
                continue
            if material_changed:
                important_diff = structured_diff(old_important, new_important)
                awaiting_new = bool(
                    classification is not None and classification.result_metadata.get("notify_new")
                )
                if baseline_before and important_diff and not awaiting_new and not released_new:
                    self._add_domain_change(
                        session,
                        event,
                        NotificationType.IMPORTANT_CHANGE,
                        important_diff,
                        categories[event.id],
                    )

        for event in db_events:
            if event.source_id in observed_all and event.source_id not in merged:
                event.last_seen_at = now
                event.missing_streak = 0

        for event in db_events:
            if event.source_theme not in complete_themes:
                continue
            if not event.is_available:
                continue
            if event.source_id in observed_by_theme.get(event.source_theme, set()):
                continue
            event.missing_streak += 1
            if event.missing_streak < 2:
                continue
            old_localization_data = self._official_localization_data(localizations[event.id])
            old_important = self._important_snapshot(event, old_localization_data)
            event.is_available = False
            event.state = EventState.HIDDEN
            event.revision += 1
            new_important = self._important_snapshot(event, old_localization_data)
            event.important_hash = important_hash(new_important)
            hidden += 1
            classification = classifications.get(event.id)
            awaiting_new = bool(
                classification is not None and classification.result_metadata.get("notify_new")
            )
            if baseline_before and not awaiting_new:
                self._add_domain_change(
                    session,
                    event,
                    NotificationType.CANCELLATION,
                    structured_diff(old_important, new_important),
                    categories[event.id],
                )
        return created, updated_events, hidden

    @staticmethod
    async def _release_ready_new_event(
        session: AsyncSession,
        event: Event,
        classification: EventClassification,
        categories: list[str],
        now: datetime,
    ) -> bool:
        if (
            not event.is_available
            or not classification.result_metadata.get("notify_new")
            or classification.status
            not in (ClassificationStatus.COMPLETED, ClassificationStatus.FALLBACK)
        ):
            return False
        existing = await session.scalar(
            select(DomainChange.id).where(
                DomainChange.event_id == event.id,
                DomainChange.notification_type == NotificationType.NEW_EVENT,
            )
        )
        if existing is None:
            classified_at = classification.classified_at
            session.add(
                DomainChange(
                    event_id=event.id,
                    event_revision=event.revision,
                    notification_type=NotificationType.NEW_EVENT,
                    old_categories=[],
                    new_categories=sorted(categories),
                    change_data={
                        "new": True,
                        "classified_at": (
                            classified_at.astimezone(UTC).isoformat()
                            if classified_at is not None
                            else now.isoformat()
                        ),
                    },
                )
            )
        classification.result_metadata = {
            **classification.result_metadata,
            "notify_new": False,
        }
        return True

    @staticmethod
    def _queue_classification(
        session: AsyncSession,
        event: Event,
        classification: EventClassification | None,
        *,
        semantic_hash_value: str,
        notify_new: bool,
        now: datetime,
    ) -> EventClassification:
        preserved_notify_new = notify_new
        if classification is not None:
            preserved_notify_new = preserved_notify_new or bool(
                classification.result_metadata.get("notify_new")
            )
        metadata: dict[str, object] = {
            "notify_new": preserved_notify_new,
            "queued_revision": event.revision,
        }
        if classification is None:
            classification = EventClassification(
                event_id=event.id,
                status=ClassificationStatus.PENDING,
                semantic_hash=semantic_hash_value,
                attempts=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("0"),
                first_attempt_at=now,
                next_attempt_at=now,
                result_metadata=metadata,
                created_at=now,
            )
            session.add(classification)
            return classification

        classification.status = ClassificationStatus.PENDING
        classification.semantic_hash = semantic_hash_value
        classification.model = None
        classification.prompt_version = None
        classification.attempts = 0
        classification.input_tokens = 0
        classification.output_tokens = 0
        classification.cost_usd = Decimal("0")
        classification.first_attempt_at = now
        classification.next_attempt_at = now
        classification.classified_at = None
        classification.last_error_code = None
        classification.result_metadata = metadata
        return classification

    @staticmethod
    def _localization_data(
        rows: Mapping[Locale, EventLocalization],
    ) -> dict[str, dict[str, str | None]]:
        return {
            locale.value: {
                "title": row.title,
                "description": row.description,
                "audience": row.audience,
                "speaker": row.speaker,
                "details_url": row.details_url,
            }
            for locale, row in rows.items()
        }

    @staticmethod
    def _official_localization_data(
        rows: Mapping[Locale, EventLocalization],
    ) -> dict[str, dict[str, str | None]]:
        return SyncService._localization_data(
            {
                locale: row
                for locale, row in rows.items()
                if row.translation_source is TranslationSource.OFFICIAL
            }
        )

    @staticmethod
    def _important_snapshot(
        event: Event, localizations: Mapping[str, Mapping[str, str | None]]
    ) -> dict[str, object]:
        return {
            "address": event.address,
            "format": event.event_format.value,
            "is_available": event.is_available,
            "registration_url": event.registration_url,
            "starts_at": event.starts_at.astimezone(UTC).isoformat(),
            "state": event.state.value,
            "titles": {
                locale: localization.get("title")
                for locale, localization in sorted(localizations.items())
            },
        }

    @staticmethod
    def _add_domain_change(
        session: AsyncSession,
        event: Event,
        notification_type: NotificationType,
        change_data: Mapping[str, object],
        categories: list[str],
    ) -> None:
        session.add(
            DomainChange(
                event_id=event.id,
                event_revision=event.revision,
                notification_type=notification_type,
                old_categories=sorted(categories),
                new_categories=sorted(categories),
                change_data=dict(change_data),
            )
        )

    async def _apply_alerts(
        self,
        session: AsyncSession,
        alerts: Mapping[str, _AlertData],
        resolved_alerts: set[str],
        now: datetime,
    ) -> None:
        if resolved_alerts:
            await session.execute(
                update(AdminAlert)
                .where(
                    AdminAlert.fingerprint.in_(resolved_alerts),
                    AdminAlert.resolved_at.is_(None),
                )
                .values(resolved_at=now, last_seen_at=now)
            )
        if not alerts:
            return
        existing = {
            alert.fingerprint: alert
            for alert in (
                await session.scalars(
                    select(AdminAlert).where(
                        AdminAlert.fingerprint.in_(alerts),
                        AdminAlert.resolved_at.is_(None),
                    )
                )
            ).all()
        }
        for fingerprint, alert_data in alerts.items():
            alert = existing.get(fingerprint)
            if alert is None:
                session.add(
                    AdminAlert(
                        kind=alert_data.kind,
                        fingerprint=fingerprint,
                        details=alert_data.details,
                        occurrence_count=1,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                alert.details = alert_data.details
                alert.occurrence_count += 1
                alert.last_seen_at = now
