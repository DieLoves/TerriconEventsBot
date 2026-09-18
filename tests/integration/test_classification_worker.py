from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.classification import (
    ClassificationWorker,
    OpenAIPricing,
)
from terricon_events_bot.domain.ai import (
    ClassificationInput,
    ClassificationOutput,
    ClassificationResult,
    OpenAIUsageData,
    TranslationInput,
    TranslationOutput,
    TranslationResult,
)
from terricon_events_bot.domain.enums import (
    CategorySlug,
    ClassificationStatus,
    EventFormat,
    Locale,
    NotificationType,
    OpenAIOperation,
    SourceTheme,
    TranslationSource,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    AdminAlert,
    DomainChange,
    Event,
    EventCategory,
    EventClassification,
    EventLocalization,
    OpenAIUsage,
)
from terricon_events_bot.infrastructure.openai_adapter import OpenAIAdapterError

pytestmark = pytest.mark.postgres

NOW = datetime(2030, 2, 10, 12, tzinfo=UTC)
PRICING = OpenAIPricing(Decimal("0.20"), Decimal("1.20"))


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeAI:
    def __init__(self) -> None:
        self.classification_error: OpenAIAdapterError | None = None
        self.classification_calls: list[ClassificationInput] = []
        self.translation_calls: list[TranslationInput] = []
        self.categories = (CategorySlug.DEVELOPMENT_IT, CategorySlug.AI_DATA)

    async def classify_event(self, value: ClassificationInput) -> ClassificationResult:
        self.classification_calls.append(value)
        if self.classification_error is not None:
            raise self.classification_error
        return ClassificationResult(
            output=ClassificationOutput(categories=self.categories),
            usage=OpenAIUsageData(input_tokens=120, output_tokens=8),
            model="gpt-5.6-luna",
            prompt_version="classification-v1",
            duration_ms=125,
        )

    async def translate_missing_locale(self, value: TranslationInput) -> TranslationResult:
        self.translation_calls.append(value)
        return TranslationResult(
            output=TranslationOutput(
                title=f"{value.title} KZ",
                description=value.description,
                audience=value.audience,
                speaker=value.speaker,
            ),
            usage=OpenAIUsageData(input_tokens=80, output_tokens=20),
            model="gpt-5.6-luna",
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
        model="gpt-5.6-luna",
        monthly_budget_usd=budget,
        pricing=PRICING,
        clock=clock,
    )


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
        usage = await session.scalar(select(OpenAIUsage))
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
    ai.classification_error = OpenAIAdapterError(
        "invalid_output", OpenAIUsageData(input_tokens=50, output_tokens=5)
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
        usage = await session.scalar(select(OpenAIUsage))

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
                OpenAIUsage(
                    event_id=None,
                    operation=OpenAIOperation.CLASSIFICATION,
                    model="gpt-5.6-luna",
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
    clock.value += timedelta(minutes=6)
    second = await service.run_batch()

    async with session_factory() as session:
        alerts = (await session.scalars(select(AdminAlert))).all()
        statuses = (await session.scalars(select(EventClassification.status))).all()

    assert first.retried == 2 and second.retried == 2
    assert not ai.classification_calls
    assert statuses == [ClassificationStatus.RETRY_WAIT, ClassificationStatus.RETRY_WAIT]
    assert len(alerts) == 1
    assert alerts[0].kind == "openai_budget_exhausted"
    assert alerts[0].occurrence_count == 4


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
        operations = (
            await session.scalars(select(OpenAIUsage.operation).order_by(OpenAIUsage.id))
        ).all()

    assert result.completed == 1
    assert len(ai.translation_calls) == 1
    assert localization is not None
    assert localization.translation_source is TranslationSource.MACHINE
    assert localization.title.endswith("KZ")
    assert operations == [OpenAIOperation.CLASSIFICATION, OpenAIOperation.TRANSLATION]


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
    assert alert.fingerprint == "openai:monthly_budget_exhausted"


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
