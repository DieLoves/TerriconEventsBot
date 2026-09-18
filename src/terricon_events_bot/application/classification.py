from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.domain.ai import (
    ClassificationInput,
    ClassificationLocalization,
    ClassificationResult,
    OpenAIUsageData,
    TranslationInput,
    TranslationResult,
)
from terricon_events_bot.domain.enums import (
    CategorySlug,
    ClassificationStatus,
    Locale,
    NotificationType,
    OpenAIOperation,
    TranslationSource,
)
from terricon_events_bot.domain.event_changes import canonical_hash
from terricon_events_bot.infrastructure.models import (
    AdminAlert,
    DomainChange,
    Event,
    EventCategory,
    EventClassification,
    EventLocalization,
    OpenAIUsage,
)
from terricon_events_bot.infrastructure.openai_adapter import (
    CLASSIFICATION_PROMPT_VERSION,
    OpenAIAdapterError,
)

Clock = Callable[[], datetime]

_FALLBACK_AFTER = timedelta(hours=24)
_CLAIM_LEASE = timedelta(minutes=10)
_MAX_RETRY_DELAY = timedelta(hours=6)
_COST_QUANTUM = Decimal("0.000000001")
_MILLION = Decimal("1000000")
_BUDGET_ALERT_FINGERPRINT = "openai:monthly_budget_exhausted"


class EventAI(Protocol):
    async def classify_event(self, value: ClassificationInput) -> ClassificationResult: ...

    async def translate_missing_locale(self, value: TranslationInput) -> TranslationResult: ...


@dataclass(frozen=True, slots=True)
class OpenAIPricing:
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal

    def __post_init__(self) -> None:
        if (
            not self.input_per_million_usd.is_finite()
            or not self.output_per_million_usd.is_finite()
            or self.input_per_million_usd < 0
            or self.output_per_million_usd < 0
        ):
            raise ValueError("OpenAI prices must be finite and nonnegative")


@dataclass(frozen=True, slots=True)
class ClassificationBatchResult:
    claimed: int
    completed: int
    retried: int
    fallback: int
    stale: int


@dataclass(frozen=True, slots=True)
class _ReservedJob:
    event_id: int
    semantic_hash: str


@dataclass(frozen=True, slots=True)
class _JobSnapshot:
    event_id: int
    semantic_hash: str
    first_attempt_at: datetime
    input: ClassificationInput | None


def calculate_openai_cost(usage: OpenAIUsageData, pricing: OpenAIPricing) -> Decimal:
    cost = (
        Decimal(usage.input_tokens) * pricing.input_per_million_usd
        + Decimal(usage.output_tokens) * pricing.output_per_million_usd
    ) / _MILLION
    return cost.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)


class ClassificationWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ai: EventAI,
        *,
        model: str,
        monthly_budget_usd: Decimal,
        pricing: OpenAIPricing,
        clock: Clock = lambda: datetime.now(UTC),
        logger: logging.Logger | None = None,
    ) -> None:
        if not monthly_budget_usd.is_finite() or monthly_budget_usd < 0:
            raise ValueError("OpenAI monthly budget must be finite and nonnegative")
        self._session_factory = session_factory
        self._ai = ai
        self._model = model
        self._monthly_budget_usd = monthly_budget_usd
        self._pricing = pricing
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    async def run_batch(self, limit: int = 20) -> ClassificationBatchResult:
        if limit <= 0:
            raise ValueError("Classification batch limit must be positive")
        counts = {
            ClassificationStatus.COMPLETED: 0,
            ClassificationStatus.RETRY_WAIT: 0,
            ClassificationStatus.FALLBACK: 0,
        }
        stale = 0
        claimed = 0
        while claimed < limit:
            jobs = await self._reserve_due_jobs(1, self._normalized_now())
            if not jobs:
                break
            claimed += 1
            outcome = await self._process_job(jobs[0])
            if outcome is None:
                stale += 1
            else:
                counts[outcome] += 1
        return ClassificationBatchResult(
            claimed=claimed,
            completed=counts[ClassificationStatus.COMPLETED],
            retried=counts[ClassificationStatus.RETRY_WAIT],
            fallback=counts[ClassificationStatus.FALLBACK],
            stale=stale,
        )

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("ClassificationWorker clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    async def _reserve_due_jobs(self, limit: int, now: datetime) -> list[_ReservedJob]:
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(EventClassification)
                        .where(
                            EventClassification.status.in_(
                                (ClassificationStatus.PENDING, ClassificationStatus.RETRY_WAIT)
                            ),
                            or_(
                                EventClassification.next_attempt_at.is_(None),
                                EventClassification.next_attempt_at <= now,
                            ),
                        )
                        .order_by(
                            EventClassification.next_attempt_at.asc().nullsfirst(),
                            EventClassification.event_id,
                        )
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                jobs = [_ReservedJob(row.event_id, row.semantic_hash) for row in rows]
                for row in rows:
                    row.next_attempt_at = now + _CLAIM_LEASE
                return jobs

    async def _process_job(self, job: _ReservedJob) -> ClassificationStatus | None:
        now = self._normalized_now()
        snapshot = await self._load_snapshot(job)
        if snapshot is None:
            return None
        if snapshot.input is None:
            return await self._record_failure(job, "missing_localization", now, attempted=True)
        if now - snapshot.first_attempt_at >= _FALLBACK_AFTER:
            return await self._record_failure(job, "deadline_exceeded", now, attempted=False)
        if await self._budget_exhausted(now):
            return await self._record_failure(job, "budget_exhausted", now, attempted=True)

        try:
            result = await self._ai.classify_event(snapshot.input)
        except OpenAIAdapterError as error:
            return await self._record_failure(
                job,
                error.code,
                self._normalized_now(),
                attempted=True,
                usage=error.usage,
            )
        except Exception:
            self._logger.exception(
                "Unexpected OpenAI classification failure",
                extra={"error_code": "unexpected_openai_error", "event_id": job.event_id},
            )
            return await self._record_failure(
                job, "unexpected_error", self._normalized_now(), attempted=True
            )

        completed_at = self._normalized_now()
        persisted = await self._record_success(job, result, completed_at)
        if persisted:
            await self._translate_missing_locale(job.event_id)
            return ClassificationStatus.COMPLETED
        return None

    async def _load_snapshot(self, job: _ReservedJob) -> _JobSnapshot | None:
        async with self._session_factory() as session:
            classification = await session.get(EventClassification, job.event_id)
            event = await session.get(Event, job.event_id)
            if (
                classification is None
                or event is None
                or classification.semantic_hash != job.semantic_hash
                or classification.status
                not in (ClassificationStatus.PENDING, ClassificationStatus.RETRY_WAIT)
            ):
                return None
            rows = (
                await session.scalars(
                    select(EventLocalization)
                    .where(
                        EventLocalization.event_id == job.event_id,
                        EventLocalization.translation_source == TranslationSource.OFFICIAL,
                    )
                    .order_by(EventLocalization.locale)
                )
            ).all()
            first_attempt_at = classification.first_attempt_at or classification.created_at
            return _JobSnapshot(
                event_id=job.event_id,
                semantic_hash=job.semantic_hash,
                first_attempt_at=first_attempt_at.astimezone(UTC),
                input=(
                    ClassificationInput(
                        localizations=tuple(
                            ClassificationLocalization(
                                locale=row.locale,
                                title=row.title,
                                description=row.description,
                                audience=row.audience,
                                speaker=row.speaker,
                            )
                            for row in rows
                        )
                    )
                    if rows
                    else None
                ),
            )

    async def _budget_exhausted(self, now: datetime) -> bool:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self._session_factory() as session:
            spent = await self._monthly_spend(session, month_start)
        return Decimal(spent or 0) >= self._monthly_budget_usd

    async def _record_failure(
        self,
        job: _ReservedJob,
        error_code: str,
        now: datetime,
        *,
        attempted: bool,
        usage: OpenAIUsageData | None = None,
    ) -> ClassificationStatus | None:
        cost = calculate_openai_cost(usage, self._pricing) if usage is not None else None
        async with self._session_factory() as session:
            async with session.begin():
                classification = await session.get(
                    EventClassification, job.event_id, with_for_update=True
                )
                if usage is not None and cost is not None:
                    event_exists = await session.get(Event, job.event_id) is not None
                    session.add(
                        OpenAIUsage(
                            event_id=job.event_id if event_exists else None,
                            operation=OpenAIOperation.CLASSIFICATION,
                            model=self._model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cost_usd=cost,
                            occurred_at=now,
                        )
                    )
                    await self._refresh_budget_alert(session, now)
                if not self._matches_job(classification, job):
                    return None
                assert classification is not None
                if classification.first_attempt_at is None:
                    classification.first_attempt_at = now
                if attempted:
                    classification.attempts += 1
                if usage is not None and cost is not None:
                    classification.input_tokens += usage.input_tokens
                    classification.output_tokens += usage.output_tokens
                    classification.cost_usd += cost
                await self._replace_categories(session, job.event_id, (CategorySlug.OTHER,))
                deadline = classification.first_attempt_at.astimezone(UTC) + _FALLBACK_AFTER
                fallback = now >= deadline
                classification.status = (
                    ClassificationStatus.FALLBACK if fallback else ClassificationStatus.RETRY_WAIT
                )
                classification.model = self._model
                classification.prompt_version = CLASSIFICATION_PROMPT_VERSION
                classification.classified_at = now if fallback else None
                classification.last_error_code = error_code
                classification.next_attempt_at = (
                    None if fallback else now + self._retry_delay(max(1, classification.attempts))
                )
                classification.result_metadata = {
                    **classification.result_metadata,
                    "fallback_reason": error_code if fallback else None,
                }
                if error_code == "budget_exhausted":
                    await self._upsert_budget_alert(session, now)
                if fallback:
                    await self._update_change_categories(
                        session, classification, (CategorySlug.OTHER,)
                    )
                    await self._release_new_event(
                        session, classification, (CategorySlug.OTHER,), now
                    )
                return classification.status

    async def _record_success(
        self,
        job: _ReservedJob,
        result: ClassificationResult,
        now: datetime,
    ) -> bool:
        cost = calculate_openai_cost(result.usage, self._pricing)
        async with self._session_factory() as session:
            async with session.begin():
                classification = await session.get(
                    EventClassification, job.event_id, with_for_update=True
                )
                event_exists = await session.get(Event, job.event_id) is not None
                session.add(
                    OpenAIUsage(
                        event_id=job.event_id if event_exists else None,
                        operation=OpenAIOperation.CLASSIFICATION,
                        model=result.model,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cost_usd=cost,
                        occurred_at=now,
                    )
                )
                await self._refresh_budget_alert(session, now)
                if not self._matches_job(classification, job):
                    return False
                assert classification is not None
                classification.status = ClassificationStatus.COMPLETED
                classification.model = result.model
                classification.prompt_version = result.prompt_version
                classification.attempts += 1
                classification.input_tokens += result.usage.input_tokens
                classification.output_tokens += result.usage.output_tokens
                classification.cost_usd += cost
                classification.next_attempt_at = None
                classification.classified_at = now
                classification.last_error_code = None
                classification.result_metadata = {
                    **classification.result_metadata,
                    "categories": [category.value for category in result.output.categories],
                    "duration_ms": result.duration_ms,
                    "fallback_reason": None,
                }
                await self._replace_categories(session, job.event_id, result.output.categories)
                await self._update_change_categories(
                    session, classification, result.output.categories
                )
                await self._release_new_event(
                    session, classification, result.output.categories, now
                )
                return True

    async def _translate_missing_locale(self, event_id: int) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(EventLocalization)
                    .where(EventLocalization.event_id == event_id)
                    .order_by(EventLocalization.locale)
                )
            ).all()
            if len(rows) != 1 or rows[0].translation_source is not TranslationSource.OFFICIAL:
                return
            source = rows[0]
            target = Locale.KZ if source.locale is Locale.RU else Locale.RU
            source_hash = source.content_hash
            value = TranslationInput(
                source_locale=source.locale,
                target_locale=target,
                title=source.title,
                description=source.description,
                audience=source.audience,
                speaker=source.speaker,
            )
            details_url = source.details_url
        now = self._normalized_now()
        if await self._budget_exhausted(now):
            async with self._session_factory() as session:
                async with session.begin():
                    await self._upsert_budget_alert(session, now)
            return
        try:
            result = await self._ai.translate_missing_locale(value)
        except OpenAIAdapterError as error:
            if error.usage is not None:
                await self._record_translation_usage(event_id, error.usage, self._normalized_now())
            self._logger.warning(
                "OpenAI event translation failed",
                extra={"error_code": error.code, "event_id": event_id},
            )
            return
        except Exception:
            self._logger.exception(
                "Unexpected OpenAI translation failure",
                extra={"error_code": "unexpected_openai_error", "event_id": event_id},
            )
            return

        cost = calculate_openai_cost(result.usage, self._pricing)
        completed_at = self._normalized_now()
        async with self._session_factory() as session:
            async with session.begin():
                current_source = await session.get(
                    EventLocalization, (event_id, value.source_locale)
                )
                existing_target = await session.get(
                    EventLocalization, (event_id, value.target_locale)
                )
                event_exists = await session.get(Event, event_id) is not None
                session.add(
                    OpenAIUsage(
                        event_id=event_id if event_exists else None,
                        operation=OpenAIOperation.TRANSLATION,
                        model=result.model,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cost_usd=cost,
                        occurred_at=completed_at,
                    )
                )
                await self._refresh_budget_alert(session, completed_at)
                if (
                    current_source is None
                    or current_source.translation_source is not TranslationSource.OFFICIAL
                    or current_source.content_hash != source_hash
                    or existing_target is not None
                ):
                    return
                translated = result.output
                localization_data = {
                    "audience": translated.audience,
                    "description": translated.description,
                    "details_url": details_url,
                    "speaker": translated.speaker,
                    "title": translated.title,
                }
                session.add(
                    EventLocalization(
                        event_id=event_id,
                        locale=value.target_locale,
                        title=translated.title,
                        description=translated.description,
                        audience=translated.audience,
                        speaker=translated.speaker,
                        details_url=details_url,
                        translation_source=TranslationSource.MACHINE,
                        content_hash=canonical_hash(localization_data),
                        translated_at=completed_at,
                    )
                )

    async def _record_translation_usage(
        self,
        event_id: int,
        usage: OpenAIUsageData,
        now: datetime,
    ) -> None:
        cost = calculate_openai_cost(usage, self._pricing)
        async with self._session_factory() as session:
            async with session.begin():
                event_exists = await session.get(Event, event_id) is not None
                session.add(
                    OpenAIUsage(
                        event_id=event_id if event_exists else None,
                        operation=OpenAIOperation.TRANSLATION,
                        model=self._model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=cost,
                        occurred_at=now,
                    )
                )
                await self._refresh_budget_alert(session, now)

    @staticmethod
    async def _replace_categories(
        session: AsyncSession,
        event_id: int,
        categories: Sequence[CategorySlug],
    ) -> None:
        await session.execute(delete(EventCategory).where(EventCategory.event_id == event_id))
        session.add_all(
            EventCategory(event_id=event_id, category=category) for category in categories
        )

    @staticmethod
    async def _update_change_categories(
        session: AsyncSession,
        classification: EventClassification,
        categories: Sequence[CategorySlug],
    ) -> None:
        event = await session.get(Event, classification.event_id)
        if event is None:
            return
        queued_revision = classification.result_metadata.get("queued_revision")
        first_revision = (
            queued_revision
            if isinstance(queued_revision, int)
            and not isinstance(queued_revision, bool)
            and queued_revision >= 1
            else event.revision
        )
        values = sorted(category.value for category in categories)
        await session.execute(
            update(DomainChange)
            .where(
                DomainChange.event_id == classification.event_id,
                DomainChange.event_revision >= first_revision,
                DomainChange.notification_type == NotificationType.IMPORTANT_CHANGE,
            )
            .values(new_categories=values)
        )

    @staticmethod
    async def _release_new_event(
        session: AsyncSession,
        classification: EventClassification,
        categories: Sequence[CategorySlug],
        now: datetime,
    ) -> None:
        if not classification.result_metadata.get("notify_new"):
            return
        event = await session.get(Event, classification.event_id)
        if event is None or not event.is_available:
            return
        existing = await session.scalar(
            select(DomainChange.id).where(
                DomainChange.event_id == event.id,
                DomainChange.notification_type == NotificationType.NEW_EVENT,
            )
        )
        if existing is None:
            values = sorted(category.value for category in categories)
            session.add(
                DomainChange(
                    event_id=event.id,
                    event_revision=event.revision,
                    notification_type=NotificationType.NEW_EVENT,
                    old_categories=[],
                    new_categories=values,
                    change_data={"new": True, "classified_at": now.isoformat()},
                )
            )
        classification.result_metadata = {
            **classification.result_metadata,
            "notify_new": False,
        }

    async def _upsert_budget_alert(self, session: AsyncSession, now: datetime) -> None:
        details = {
            "budget_usd": str(self._monthly_budget_usd),
            "model": self._model,
        }
        statement = pg_insert(AdminAlert).values(
            kind="openai_budget_exhausted",
            fingerprint=_BUDGET_ALERT_FINGERPRINT,
            details=details,
            occurrence_count=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[AdminAlert.fingerprint],
                index_where=AdminAlert.resolved_at.is_(None),
                set_={
                    "details": details,
                    "occurrence_count": AdminAlert.occurrence_count + 1,
                    "last_seen_at": now,
                },
            )
        )

    async def _refresh_budget_alert(self, session: AsyncSession, now: datetime) -> None:
        await session.flush()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent = await self._monthly_spend(session, month_start)
        if spent >= self._monthly_budget_usd:
            await self._upsert_budget_alert(session, now)
            return
        await session.execute(
            update(AdminAlert)
            .where(
                AdminAlert.fingerprint == _BUDGET_ALERT_FINGERPRINT,
                AdminAlert.resolved_at.is_(None),
            )
            .values(resolved_at=now, last_seen_at=now)
        )

    @staticmethod
    async def _monthly_spend(session: AsyncSession, month_start: datetime) -> Decimal:
        spent = await session.scalar(
            select(func.coalesce(func.sum(OpenAIUsage.cost_usd), Decimal("0"))).where(
                OpenAIUsage.occurred_at >= month_start,
                OpenAIUsage.occurred_at < ClassificationWorker._next_month(month_start),
            )
        )
        return Decimal(spent or 0)

    @staticmethod
    def _matches_job(classification: EventClassification | None, job: _ReservedJob) -> bool:
        return bool(
            classification is not None
            and classification.semantic_hash == job.semantic_hash
            and classification.status
            in (ClassificationStatus.PENDING, ClassificationStatus.RETRY_WAIT)
        )

    @staticmethod
    def _retry_delay(attempts: int) -> timedelta:
        exponent = min(max(attempts - 1, 0), 8)
        return min(timedelta(minutes=5 * (2**exponent)), _MAX_RETRY_DELAY)

    @staticmethod
    def _next_month(month_start: datetime) -> datetime:
        if month_start.month == 12:
            return month_start.replace(year=month_start.year + 1, month=1)
        return month_start.replace(month=month_start.month + 1)
