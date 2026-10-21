from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.classification import (
    AIPricing,
    ClassificationWorker,
)
from terricon_events_bot.domain.ai import (
    AIAdapterError,
    AIUsageData,
    ClassificationInput,
    ClassificationOutput,
    ClassificationResult,
    TranslationInput,
    TranslationOutput,
    TranslationResult,
)
from terricon_events_bot.domain.enums import (
    AIOperation,
    CategorySlug,
    ClassificationStatus,
    EventFormat,
    Locale,
    NotificationType,
    SourceTheme,
    TranslationSource,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    AdminAlert,
    AIUsage,
    DomainChange,
    Event,
    EventCategory,
    EventClassification,
    EventLocalization,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2030, 2, 10, 12, tzinfo=UTC)
PRICING = AIPricing(Decimal("0.20"), Decimal("1.20"))


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeAI:
    def __init__(self) -> None:
        self.classification_error: AIAdapterError | None = None
        self.translation_error: AIAdapterError | None = None
        self.classification_calls: list[ClassificationInput] = []
        self.translation_calls: list[TranslationInput] = []
        self.categories = (CategorySlug.DEVELOPMENT_IT, CategorySlug.AI_DATA)

    async def classify_event(self, value: ClassificationInput) -> ClassificationResult:
        self.classification_calls.append(value)
        if self.classification_error is not None:
            raise self.classification_error
        return ClassificationResult(
            output=ClassificationOutput(categories=self.categories),
            usage=AIUsageData(input_tokens=120, output_tokens=8),
            model="openai/gpt-oss-120b",
            prompt_version="classification-v1",
            duration_ms=125,
        )

    async def translate_missing_locale(self, value: TranslationInput) -> TranslationResult:
        self.translation_calls.append(value)
        if self.translation_error is not None:
            raise self.translation_error
        return TranslationResult(
            output=TranslationOutput(
                title=f"{value.title} KZ",
                description=value.description,
                audience=value.audience,
                speaker=value.speaker,
            ),
            usage=AIUsageData(input_tokens=80, output_tokens=20),
            model="openai/gpt-oss-120b",
            prompt_version="translation-v1",
            duration_ms=200,
        )


class BlockingAI(FakeAI):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def classify_event(self, value: ClassificationInput) -> ClassificationResult:
        self.started.set()
        await self.release.wait()
        return await super().classify_event(value)


class FirstCallBlockingAI(FakeAI):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.invocations = 0

    async def classify_event(self, value: ClassificationInput) -> ClassificationResult:
        self.invocations += 1
        if self.invocations == 1:
            self.started.set()
            await self.release.wait()
        return await super().classify_event(value)


@pytest.fixture
async def classification_database(
    migrated_database_url: str,
) -> Iterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE events, admin_alerts, openai_usage RESTART IDENTITY CASCADE")
        )
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE events, admin_alerts, openai_usage RESTART IDENTITY CASCADE")
            )
        await engine.dispose()


async def seed_job(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: int,
    *,
    notify_new: bool = True,
    locales: tuple[Locale, ...] = (Locale.RU, Locale.KZ),
) -> int:
    async with session_factory() as session:
        async with session.begin():
            event = Event(
                source_id=source_id,
                source_theme=SourceTheme.IT,
                starts_at=NOW + timedelta(days=1),
                event_format=EventFormat.OFFLINE,
                event_languages=["ru"],
                semantic_hash=f"{source_id:064d}"[-64:],
                important_hash="a" * 64,
                revision=1,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            session.add(event)
            await session.flush()
            for locale in locales:
                session.add(
                    EventLocalization(
                        event_id=event.id,
                        locale=locale,
                        title=f"Event {source_id} {locale.value}",
                        description="Description",
                        translation_source=TranslationSource.OFFICIAL,
                        content_hash=("b" if locale is Locale.RU else "c") * 64,
                    )
                )
            session.add(
                EventClassification(
                    event_id=event.id,
                    status=ClassificationStatus.PENDING,
                    semantic_hash=event.semantic_hash,
                    first_attempt_at=NOW,
                    next_attempt_at=NOW,
                    result_metadata={"notify_new": notify_new, "queued_revision": 1},
                    created_at=NOW,
                )
            )
            return event.id


def worker(
    session_factory: async_sessionmaker[AsyncSession],
    ai: FakeAI,
    clock: MutableClock,
    *,
    budget: Decimal = Decimal("5"),
) -> ClassificationWorker:
    return ClassificationWorker(
        session_factory,
        ai,
        model="openai/gpt-oss-120b",
        monthly_budget_usd=budget,
        pricing=PRICING,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_groq_rate_limit_stops_batch_and_persists_server_retry_after(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = classification_database
    first = await seed_job(factory, 901)
    second = await seed_job(factory, 902)
    ai = FakeAI()
    error = AIAdapterError("rate_limit")
    error.retry_after_seconds = 3600
    ai.classification_error = error
    clock = MutableClock(NOW)

    result = await worker(factory, ai, clock).run_batch()

    assert result.claimed == 1
    assert result.retried == 1
    async with factory() as session:
        first_job = await session.get(EventClassification, first)
        second_job = await session.get(EventClassification, second)
        assert first_job is not None and second_job is not None
        assert first_job.next_attempt_at == NOW + timedelta(hours=1)
        assert second_job.attempts == 0
        assert second_job.status is ClassificationStatus.PENDING


@pytest.mark.asyncio
async def test_long_groq_retry_after_does_not_postpone_24_hour_fallback(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = classification_database
    event_id = await seed_job(factory, 903)
    ai = FakeAI()
    error = AIAdapterError("rate_limit")
    error.retry_after_seconds = 172800
    ai.classification_error = error
    clock = MutableClock(NOW)
    service = worker(factory, ai, clock)

    await service.run_batch()

    async with factory() as session:
        classification = await session.get(EventClassification, event_id)
        assert classification is not None
        assert classification.next_attempt_at == NOW + timedelta(hours=24)

    clock.value = NOW + timedelta(hours=24)
    result = await service.run_batch()
    assert result.fallback == 1


@pytest.mark.asyncio
async def test_translation_rate_limit_survives_restart_without_reclassifying(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = classification_database
    event_id = await seed_job(factory, 904, locales=(Locale.RU,))
    ai = FakeAI()
    ai.translation_error = AIAdapterError("rate_limit", retry_after_seconds=3600)
    clock = MutableClock(NOW)

    await worker(factory, ai, clock).run_batch()

    async with factory() as session:
        classification = await session.get(EventClassification, event_id)
        assert classification is not None
        assert classification.status is ClassificationStatus.COMPLETED
        assert classification.next_attempt_at == NOW + timedelta(hours=1)

    restarted = worker(factory, ai, clock)
    clock.value = NOW + timedelta(minutes=59)
    await restarted.run_batch()
    assert len(ai.translation_calls) == 1
    ai.translation_error = None
    clock.value = NOW + timedelta(hours=1)
    await restarted.run_batch()

    assert len(ai.classification_calls) == 1
    assert len(ai.translation_calls) == 2
    async with factory() as session:
        translated = await session.get(EventLocalization, (event_id, Locale.KZ))
        classification = await session.get(EventClassification, event_id)
        assert translated is not None
        assert translated.translation_source is TranslationSource.MACHINE
        assert classification is not None and classification.next_attempt_at is None
        assert await session.scalar(select(func.count(DomainChange.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("official_arrived", [False, True])
async def test_translation_retry_stops_at_deadline_or_when_official_locale_arrives(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    official_arrived: bool,
) -> None:
    _, factory = classification_database
    event_id = await seed_job(factory, 905, locales=(Locale.RU,))
    ai = FakeAI()
    ai.translation_error = AIAdapterError("rate_limit", retry_after_seconds=3600)
    clock = MutableClock(NOW)
    await worker(factory, ai, clock).run_batch()

    if official_arrived:
        async with factory() as session, session.begin():
            session.add(
                EventLocalization(
                    event_id=event_id,
                    locale=Locale.KZ,
                    title="Official title",
                    translation_source=TranslationSource.OFFICIAL,
                    content_hash="d" * 64,
                )
            )
    clock.value = NOW + timedelta(hours=1 if official_arrived else 24)
    await worker(factory, ai, clock).run_batch()

    assert len(ai.translation_calls) == 1
    assert len(ai.classification_calls) == 1
    async with factory() as session:
        classification = await session.get(EventClassification, event_id)
        assert classification is not None and classification.next_attempt_at is None
        if official_arrived:
            target = await session.get(EventLocalization, (event_id, Locale.KZ))
            assert target is not None and target.title == "Official title"


@pytest.mark.asyncio
async def test_completed_classification_updates_categories_ledger_and_releases_new_event(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    event_id = await seed_job(session_factory, 9001)
    ai = FakeAI()

    result = await worker(session_factory, ai, MutableClock()).run_batch(limit=1)

    async with session_factory() as session:
        classification = await session.get(EventClassification, event_id)
        categories = set(
            (
                await session.scalars(
                    select(EventCategory.category).where(EventCategory.event_id == event_id)
                )
            ).all()
        )
        usage = await session.scalar(select(AIUsage))
        change = await session.scalar(select(DomainChange))

    assert result.completed == 1 and result.claimed == 1
    assert classification is not None and classification.status is ClassificationStatus.COMPLETED
    assert classification.cost_usd == Decimal("0.000033600")
    assert classification.result_metadata["notify_new"] is False
    assert categories == {CategorySlug.DEVELOPMENT_IT, CategorySlug.AI_DATA}
    assert usage is not None and usage.cost_usd == Decimal("0.000033600")
    assert change is not None and change.notification_type is NotificationType.NEW_EVENT
    assert change.old_categories == []
    assert change.new_categories == ["ai_data", "development_it"]


@pytest.mark.asyncio
async def test_failure_shows_other_then_falls_back_after_24_hours(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    event_id = await seed_job(session_factory, 9002)
    ai = FakeAI()
    ai.classification_error = AIAdapterError(
        "invalid_output", AIUsageData(input_tokens=50, output_tokens=5)
    )
    clock = MutableClock()
    service = worker(session_factory, ai, clock)

    first = await service.run_batch(limit=1)
    async with session_factory() as session:
        classification = await session.get(EventClassification, event_id)
        categories = (
            await session.scalars(
                select(EventCategory.category).where(EventCategory.event_id == event_id)
            )
        ).all()
        change_count = await session.scalar(select(func.count()).select_from(DomainChange))
        usage = await session.scalar(select(AIUsage))

    assert first.retried == 1
    assert classification is not None and classification.status is ClassificationStatus.RETRY_WAIT
    assert categories == [CategorySlug.OTHER]
    assert change_count == 0
    assert usage is not None and usage.cost_usd == Decimal("0.000016000")
    assert classification.input_tokens == 50
    assert classification.output_tokens == 5

    clock.value = NOW + timedelta(hours=25)
    second = await service.run_batch(limit=1)
    async with session_factory() as session:
        classification = await session.get(EventClassification, event_id)
        change = await session.scalar(select(DomainChange))

    assert second.fallback == 1
    assert len(ai.classification_calls) == 1
    assert classification is not None and classification.status is ClassificationStatus.FALLBACK
    assert classification.last_error_code == "deadline_exceeded"
    assert change is not None and change.new_categories == ["other"]


@pytest.mark.asyncio
async def test_budget_exhaustion_pauses_calls_and_deduplicates_alert(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    await seed_job(session_factory, 9003, notify_new=False)
    await seed_job(session_factory, 9004, notify_new=False)
    async with session_factory() as session:
        async with session.begin():
            session.add(
                AIUsage(
                    event_id=None,
                    operation=AIOperation.CLASSIFICATION,
                    model="openai/gpt-oss-120b",
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=Decimal("5"),
                    occurred_at=NOW,
                )
            )
    ai = FakeAI()
    clock = MutableClock()
    service = worker(session_factory, ai, clock)

    first = await service.run_batch()
    async with session_factory() as session:
        first_seen = await session.scalar(select(AdminAlert.last_seen_at))
    clock.value += timedelta(minutes=6)
    second = await service.run_batch()

    async with session_factory() as session:
        alerts = (await session.scalars(select(AdminAlert))).all()
        statuses = (await session.scalars(select(EventClassification.status))).all()

    assert first.retried == 2 and second.retried == 2
    assert not ai.classification_calls
    assert statuses == [ClassificationStatus.RETRY_WAIT, ClassificationStatus.RETRY_WAIT]
    assert len(alerts) == 1
    assert alerts[0].kind == "groq_budget_exhausted"
    assert alerts[0].occurrence_count == 4
    assert alerts[0].last_seen_at == first_seen


@pytest.mark.asyncio
async def test_missing_locale_is_machine_translated_with_separate_usage(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    event_id = await seed_job(
        session_factory,
        9005,
        notify_new=False,
        locales=(Locale.RU,),
    )
    ai = FakeAI()

    result = await worker(session_factory, ai, MutableClock()).run_batch(limit=1)

    async with session_factory() as session:
        localization = await session.get(EventLocalization, (event_id, Locale.KZ))
        operations = (await session.scalars(select(AIUsage.operation).order_by(AIUsage.id))).all()

    assert result.completed == 1
    assert len(ai.translation_calls) == 1
    assert localization is not None
    assert localization.translation_source is TranslationSource.MACHINE
    assert localization.title.endswith("KZ")
    assert operations == [AIOperation.CLASSIFICATION, AIOperation.TRANSLATION]


@pytest.mark.asyncio
async def test_claim_lease_prevents_duplicate_paid_calls(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    await seed_job(session_factory, 9006, notify_new=False)
    ai = BlockingAI()
    first_worker = worker(session_factory, ai, MutableClock())
    second_worker = worker(session_factory, ai, MutableClock())

    first_task = asyncio.create_task(first_worker.run_batch(limit=1))
    await asyncio.wait_for(ai.started.wait(), timeout=2)
    second = await second_worker.run_batch(limit=1)
    ai.release.set()
    first = await first_task

    assert first.completed == 1
    assert second.claimed == 0
    assert len(ai.classification_calls) == 1


@pytest.mark.asyncio
async def test_batch_reserves_each_job_only_when_processing_starts(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    await seed_job(session_factory, 9007, notify_new=False)
    await seed_job(session_factory, 9008, notify_new=False)
    ai = FirstCallBlockingAI()

    first_task = asyncio.create_task(worker(session_factory, ai, MutableClock()).run_batch(limit=2))
    await asyncio.wait_for(ai.started.wait(), timeout=2)
    second = await worker(session_factory, ai, MutableClock()).run_batch(limit=1)
    ai.release.set()
    first = await first_task

    assert first.claimed == 1 and first.completed == 1
    assert second.claimed == 1 and second.completed == 1
    assert len(ai.classification_calls) == 2


@pytest.mark.asyncio
async def test_call_that_crosses_budget_creates_alert_immediately(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    await seed_job(session_factory, 9009, notify_new=False)

    result = await worker(
        session_factory,
        FakeAI(),
        MutableClock(),
        budget=Decimal("0.000020000"),
    ).run_batch(limit=1)

    async with session_factory() as session:
        alert = await session.scalar(select(AdminAlert).where(AdminAlert.resolved_at.is_(None)))

    assert result.completed == 1
    assert alert is not None
    assert alert.fingerprint == "groq:monthly_budget_exhausted"


@pytest.mark.asyncio
async def test_classification_ignores_machine_localization_input(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    event_id = await seed_job(
        session_factory,
        9010,
        notify_new=False,
        locales=(Locale.RU,),
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                EventLocalization(
                    event_id=event_id,
                    locale=Locale.KZ,
                    title="Stale machine title",
                    translation_source=TranslationSource.MACHINE,
                    content_hash="d" * 64,
                )
            )
    ai = FakeAI()

    result = await worker(session_factory, ai, MutableClock()).run_batch(limit=1)

    assert result.completed == 1
    assert len(ai.classification_calls) == 1
    assert [item.locale for item in ai.classification_calls[0].localizations] == [Locale.RU]
    assert ai.translation_calls == []


@pytest.mark.asyncio
async def test_concurrent_budget_alert_upsert_is_race_safe(
    classification_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = classification_database
    await seed_job(session_factory, 9011, notify_new=False)
    await seed_job(session_factory, 9012, notify_new=False)
    ai = FakeAI()

    results = await asyncio.gather(
        worker(session_factory, ai, MutableClock(), budget=Decimal("0")).run_batch(limit=1),
        worker(session_factory, ai, MutableClock(), budget=Decimal("0")).run_batch(limit=1),
    )

    async with session_factory() as session:
        alerts = (
            await session.scalars(select(AdminAlert).where(AdminAlert.resolved_at.is_(None)))
        ).all()

    assert sum(result.retried for result in results) == 2
    assert ai.classification_calls == []
    assert len(alerts) == 1
    assert alerts[0].occurrence_count == 2


async def test_initial_import_quota_restart_and_24h_fallback(classification_database):
    from terricon_events_bot.infrastructure.ai_quota import PostgresAIQuota, QuotaLimits

    _, factory = classification_database
    clock = MutableClock()
    ids = [await seed_job(factory, 8000 + i) for i in range(30)]

    class QuotaAI(FakeAI):
        async def classify_event(self, value):
            guard = PostgresAIQuota(
                factory, "initial-import", QuotaLimits(2, 1000, 10000, 100000), clock=clock
            )
            reservation = await guard.reserve(200)
            result = await super().classify_event(value)
            await guard.settle(reservation, result.usage, {})
            return result

    ai = QuotaAI()
    first = await worker(factory, ai, clock).run_batch(limit=100)
    assert (first.claimed, first.completed, first.retried) == (3, 2, 1)
    assert len(ai.classification_calls) == 2
    # New worker/guard instances share the persistent reservation state.
    second = await worker(factory, ai, clock).run_batch(limit=100)
    assert second.completed == 0
    assert len(ai.classification_calls) == 2
    clock.value += timedelta(seconds=60)
    recovered = await worker(factory, ai, clock).run_batch(limit=100)
    assert recovered.completed == 2
    clock.value = NOW + timedelta(hours=25)
    final = await worker(factory, ai, clock).run_batch(limit=100)
    assert final.fallback == 26
    assert len(ai.classification_calls) == 4
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(DomainChange).where(DomainChange.event_id.in_(ids))
            )
            == 30
        )


async def test_expired_unattempted_job_precedes_fresh_quota_denial(classification_database):
    _, factory = classification_database
    fresh = await seed_job(factory, 8990)
    expired = await seed_job(factory, 8991)
    async with factory() as session, session.begin():
        old = await session.get(EventClassification, expired)
        old.created_at = NOW - timedelta(hours=25)
        old.first_attempt_at = None
        old.next_attempt_at = NOW
        new = await session.get(EventClassification, fresh)
        new.next_attempt_at = None
    ai = FakeAI()
    ai.classification_error = AIAdapterError("rate_limit", retry_after_seconds=86400)
    result = await worker(factory, ai, MutableClock()).run_batch(limit=100)
    assert result.fallback == 1
    assert result.retried == 1
    assert len(ai.classification_calls) == 1
    async with factory() as session:
        old = await session.get(EventClassification, expired)
        assert old.status is ClassificationStatus.FALLBACK
        assert old.attempts == 0


async def test_first_quota_denial_does_not_reset_queued_deadline(classification_database):
    _, factory = classification_database
    event_id = await seed_job(factory, 8992)
    async with factory() as session, session.begin():
        row = await session.get(EventClassification, event_id)
        row.created_at = NOW - timedelta(hours=23, minutes=59)
        row.first_attempt_at = None
    ai = FakeAI()
    ai.classification_error = AIAdapterError("rate_limit", retry_after_seconds=86400)
    await worker(factory, ai, MutableClock()).run_batch()
    async with factory() as session:
        row = await session.get(EventClassification, event_id)
        assert row.next_attempt_at == NOW + timedelta(minutes=1)
