from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.subscriptions import (
    SubscriptionMode,
    SubscriptionService,
)
from terricon_events_bot.domain.enums import CategorySlug
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    SubscriptionCategory,
    SubscriptionSettings,
    User,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
async def subscription_database(
    migrated_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession], int]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
        user_id = await connection.scalar(
            text("INSERT INTO users (telegram_id) VALUES (3100001) RETURNING id")
        )
    assert user_id is not None
    try:
        yield engine, create_session_factory(engine), user_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
        await engine.dispose()


@pytest.mark.asyncio
async def test_replace_switches_modes_atomically_and_deduplicates_categories(
    subscription_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession], int],
) -> None:
    _, session_factory, user_id = subscription_database
    service = SubscriptionService(session_factory)

    categories = await service.replace(
        user_id,
        SubscriptionMode.CATEGORIES,
        (CategorySlug.AI_DATA, CategorySlug.DEVELOPMENT_IT, CategorySlug.AI_DATA),
    )
    all_events = await service.replace(user_id, SubscriptionMode.ALL)

    async with session_factory() as session:
        settings = await session.get(SubscriptionSettings, user_id)
        rows = (
            await session.scalars(
                select(SubscriptionCategory).where(SubscriptionCategory.user_id == user_id)
            )
        ).all()

    assert categories.categories == (
        CategorySlug.DEVELOPMENT_IT,
        CategorySlug.AI_DATA,
    )
    assert all_events.subscribe_all
    assert settings is not None and settings.subscribe_all
    assert rows == []


@pytest.mark.asyncio
async def test_toggle_category_replaces_all_and_can_clear_last_subscription(
    subscription_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession], int],
) -> None:
    _, session_factory, user_id = subscription_database
    service = SubscriptionService(session_factory)
    await service.replace(user_id, SubscriptionMode.ALL)

    selected = await service.toggle_category(user_id, CategorySlug.OTHER)
    cleared = await service.toggle_category(user_id, CategorySlug.OTHER)

    async with session_factory() as session:
        settings = await session.get(SubscriptionSettings, user_id)
        user = await session.get(User, user_id)

    assert selected.categories == (CategorySlug.OTHER,)
    assert not selected.subscribe_all
    assert cleared == await service.get(user_id)
    assert cleared.categories == ()
    assert settings is None
    assert user is not None


@pytest.mark.asyncio
async def test_replace_rejects_invalid_modes_without_mutating_state(
    subscription_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession], int],
) -> None:
    _, session_factory, user_id = subscription_database
    service = SubscriptionService(session_factory)
    await service.replace(user_id, SubscriptionMode.ALL)

    with pytest.raises(ValueError, match="only allowed"):
        await service.replace(user_id, SubscriptionMode.ALL, (CategorySlug.AI_DATA,))

    assert (await service.get(user_id)).subscribe_all


@pytest.mark.asyncio
async def test_program_toggle_is_independent_of_event_subscriptions(
    subscription_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession], int],
) -> None:
    _, session_factory, user_id = subscription_database
    service = SubscriptionService(session_factory)

    enabled = await service.toggle_programs(user_id)
    with_all = await service.toggle_all(user_id)
    with_category = await service.toggle_category(user_id, CategorySlug.AI_DATA)
    disabled = await service.toggle_programs(user_id)

    assert enabled.programs and not enabled.subscribe_all and enabled.categories == ()
    assert with_all.programs and with_all.subscribe_all
    assert with_category.programs and with_category.categories == (CategorySlug.AI_DATA,)
    assert not disabled.programs and disabled.categories == (CategorySlug.AI_DATA,)
    assert (await service.get(user_id)) == disabled
    async with session_factory() as session:
        user = await session.get(User, user_id)
    assert user is not None and not user.program_notifications
