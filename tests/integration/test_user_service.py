from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.users import TelegramPrincipal, UserService
from terricon_events_bot.domain.enums import AccessMode, Locale
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import BetaAllowlistEntry, User

pytestmark = pytest.mark.postgres


@pytest.fixture
async def user_database(
    migrated_database_url: str,
) -> Iterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE users, beta_allowlist RESTART IDENTITY CASCADE")
        )
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE users, beta_allowlist RESTART IDENTITY CASCADE")
            )
        await engine.dispose()


def service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    mode: AccessMode = AccessMode.ALLOWLIST,
    allowed: frozenset[int] = frozenset(),
    admins: frozenset[int] = frozenset(),
) -> UserService:
    return UserService(
        session_factory,
        access_mode=mode,
        allowed_telegram_ids=allowed,
        admin_telegram_ids=admins,
    )


@pytest.mark.asyncio
async def test_allowlist_denial_does_not_create_profile_and_allows_db_and_admin_entries(
    user_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = user_database
    async with session_factory() as session:
        async with session.begin():
            session.add(BetaAllowlistEntry(telegram_id=200))
    users = service(session_factory, admins=frozenset({300}))

    denied = await users.resolve(TelegramPrincipal(100, "denied", "Denied User"))
    db_allowed = await users.resolve(TelegramPrincipal(200, "@member", " Member   Name "))
    admin = await users.resolve(TelegramPrincipal(300, None, "Admin"))

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(User))

    assert denied is None
    assert count == 2
    assert db_allowed is not None
    assert db_allowed.user.username == "member"
    assert db_allowed.user.display_name == "Member Name"
    assert admin is not None and admin.is_admin


@pytest.mark.asyncio
async def test_public_concurrent_upsert_and_settings_are_persistent(
    user_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = user_database
    users = service(session_factory, mode=AccessMode.PUBLIC)
    principal = TelegramPrincipal(400, "first", "First")

    contexts = await asyncio.gather(users.resolve(principal), users.resolve(principal))
    assert all(context is not None for context in contexts)
    context = contexts[0]
    assert context is not None

    await users.set_locale(context.user.id, Locale.KZ)
    await users.complete_onboarding(context.user.id)
    assert await users.toggle_menu_images(context.user.id) is False
    assert await users.toggle_event_posters(context.user.id) is False
    await users.deactivate(context.user.id)

    async with session_factory() as session:
        rows = (await session.scalars(select(User))).all()

    assert len(rows) == 1
    assert rows[0].locale is Locale.KZ
    assert rows[0].onboarding_completed
    assert not rows[0].menu_images_enabled
    assert not rows[0].event_posters_enabled
    assert not rows[0].is_active


@pytest.mark.asyncio
async def test_revoked_known_user_is_marked_without_profile_duplication(
    user_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = user_database
    granting = service(session_factory, allowed=frozenset({500}))
    context = await granting.resolve(TelegramPrincipal(500, None, None))
    assert context is not None

    revoked = await service(session_factory).resolve(TelegramPrincipal(500, None, None))

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 500))
        count = await session.scalar(select(func.count()).select_from(User))

    assert revoked is None
    assert count == 1
    assert user is not None and not user.access_granted
