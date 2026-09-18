from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.catalog import CatalogQuery, CatalogStateStore
from terricon_events_bot.domain.catalog import (
    CatalogFilter,
    CatalogPeriod,
    CatalogSection,
    CatalogState,
    EventLanguage,
)
from terricon_events_bot.domain.enums import (
    CategorySlug,
    EventFormat,
    EventState,
    Locale,
    SourceTheme,
    TranslationSource,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    Event,
    EventCategory,
    EventLocalization,
    FsmState,
    SyncState,
    User,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2030, 1, 10, 20, tzinfo=UTC)
ALMATY = ZoneInfo("Asia/Almaty")


@pytest.fixture
async def catalog_database(
    migrated_database_url: str,
) -> Iterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users, events RESTART IDENTITY CASCADE"))
        await connection.execute(
            text(
                "UPDATE sync_state SET baseline_completed_at = :now, "
                "last_full_success_at = :now, last_attempt_at = :now WHERE id = 1"
            ),
            {"now": NOW},
        )
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE users, events RESTART IDENTITY CASCADE"))
        await engine.dispose()


async def add_event(
    session: AsyncSession,
    source_id: int,
    starts_at: datetime,
    *,
    title: str,
    category: CategorySlug = CategorySlug.DEVELOPMENT_IT,
    event_format: EventFormat = EventFormat.OFFLINE,
    languages: tuple[str, ...] = ("ru",),
    available: bool = True,
    state: EventState = EventState.ACTIVE,
    only_kz: bool = False,
) -> int:
    event = Event(
        source_id=source_id,
        source_theme=SourceTheme.IT,
        starts_at=starts_at,
        event_format=event_format,
        event_languages=list(languages),
        is_available=available,
        state=state,
        revision=1,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    session.add(event)
    await session.flush()
    locale = Locale.KZ if only_kz else Locale.RU
    session.add(
        EventLocalization(
            event_id=event.id,
            locale=locale,
            title=title,
            description=f"{title} description",
            translation_source=(
                TranslationSource.MACHINE if only_kz else TranslationSource.OFFICIAL
            ),
            content_hash=f"{source_id:064d}"[-64:],
        )
    )
    session.add(EventCategory(event_id=event.id, category=category))
    return event.id


@pytest.mark.asyncio
async def test_catalog_combines_filters_boundaries_fallback_and_stable_sorting(
    catalog_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = catalog_database
    local_day_start = datetime(2030, 1, 10, 19, tzinfo=UTC)
    async with session_factory() as session:
        async with session.begin():
            first_id = await add_event(
                session,
                1,
                local_day_start + timedelta(hours=2),
                title="First",
                category=CategorySlug.AI_DATA,
                event_format=EventFormat.HYBRID,
                languages=("en", "ru"),
            )
            second_id = await add_event(
                session,
                2,
                local_day_start + timedelta(hours=2),
                title="Second KZ fallback",
                category=CategorySlug.AI_DATA,
                event_format=EventFormat.HYBRID,
                languages=("en",),
                only_kz=True,
            )
            await add_event(
                session,
                3,
                local_day_start + timedelta(days=7),
                title="Outside seven days",
                category=CategorySlug.AI_DATA,
                event_format=EventFormat.HYBRID,
                languages=("en",),
            )
            await add_event(
                session,
                4,
                local_day_start - timedelta(seconds=1),
                title="Archived hidden",
                available=False,
                state=EventState.HIDDEN,
            )
            await add_event(
                session,
                5,
                local_day_start + timedelta(hours=3),
                title="Hidden future",
                available=False,
                state=EventState.HIDDEN,
            )

    query = CatalogQuery(session_factory, timezone=ALMATY, page_size=5)
    result = await query.list(
        CatalogFilter(
            section=CatalogSection.UPCOMING,
            period=CatalogPeriod.DAYS_7,
            category=CategorySlug.AI_DATA,
            event_format=EventFormat.HYBRID,
            language=EventLanguage.EN,
        ),
        Locale.RU,
        1,
        now=NOW,
    )
    archive = await query.list(
        CatalogFilter(CatalogSection.ARCHIVE, CatalogPeriod.DAYS_30),
        Locale.RU,
        1,
        now=NOW,
    )
    adjacent = await query.adjacent_ids(
        CatalogFilter(
            section=CatalogSection.UPCOMING,
            period=CatalogPeriod.DAYS_7,
            category=CategorySlug.AI_DATA,
            event_format=EventFormat.HYBRID,
            language=EventLanguage.EN,
        ),
        first_id,
        now=NOW,
    )

    assert [item.id for item in result.items] == [first_id, second_id]
    assert result.items[1].title == "Second KZ fallback"
    assert result.items[1].translation_source is TranslationSource.MACHINE
    assert adjacent == (None, second_id)
    assert not result.stale
    assert [item.title for item in archive.items] == ["Archived hidden"]


@pytest.mark.asyncio
async def test_catalog_paginates_clamps_and_reports_stale_data(
    catalog_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = catalog_database
    async with session_factory() as session:
        async with session.begin():
            for source_id in range(10, 15):
                await add_event(
                    session,
                    source_id,
                    NOW + timedelta(days=source_id),
                    title=f"Event {source_id}",
                )
            sync_state = await session.get(SyncState, 1, with_for_update=True)
            assert sync_state is not None
            sync_state.last_attempt_at = NOW + timedelta(hours=1)

    query = CatalogQuery(session_factory, timezone=ALMATY, page_size=2)
    result = await query.list(
        CatalogFilter.default(CatalogSection.UPCOMING),
        Locale.RU,
        99,
        now=NOW,
    )

    assert result.total == 5
    assert result.pages == 3 and result.page == 3
    assert [item.title for item in result.items] == ["Event 14"]
    assert result.stale


@pytest.mark.asyncio
async def test_catalog_state_is_durable_and_corruption_falls_back_safely(
    catalog_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = catalog_database
    async with session_factory() as session:
        async with session.begin():
            user = User(telegram_id=12345)
            session.add(user)
            await session.flush()
            user_id = user.id

    store = CatalogStateStore(session_factory)
    state = CatalogState(
        CatalogFilter(
            CatalogSection.ARCHIVE,
            CatalogPeriod.MONTHS_3,
            category=CategorySlug.BUSINESS_FINANCE,
        ),
        page=2,
    )
    await store.save(user_id, state)

    assert await store.load(user_id) == state

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(FsmState, (user_id, "catalog"), with_for_update=True)
            assert row is not None
            row.data = {"section": "broken"}

    recovered = await store.load(user_id)
    assert recovered == CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))
