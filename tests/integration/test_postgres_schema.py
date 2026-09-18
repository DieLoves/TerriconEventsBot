from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from terricon_events_bot.domain.enums import EventFormat, EventState, SourceTheme
from terricon_events_bot.infrastructure import models
from terricon_events_bot.infrastructure.database import Base
from terricon_events_bot.infrastructure.models import Event

_ = models

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_migrations_create_all_tables_and_critical_indexes(
    migrated_database_url: str,
) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            event_indexes = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_indexes("events")}
            )
            outbox_indexes = await connection.run_sync(
                lambda sync: {
                    item["name"] for item in inspect(sync).get_indexes("notification_outbox")
                }
            )
            change_indexes = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_indexes("domain_changes")}
            )

        assert set(Base.metadata.tables) <= tables
        assert {"ix_events_starts_at", "ix_events_available_starts_at"} <= event_indexes
        assert "ix_notification_outbox_pending_available" in outbox_indexes
        assert "ix_notification_outbox_claimable" in outbox_indexes
        assert "ix_domain_changes_unmaterialized" in change_indexes
    finally:
        await engine.dispose()


def test_migrated_schema_matches_sqlalchemy_metadata(
    alembic_runner: Callable[..., None],
) -> None:
    alembic_runner(command.check)


@pytest.mark.asyncio
async def test_subscription_modes_are_mutually_exclusive(migrated_database_url: str) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.begin() as connection:
            user_id = await connection.scalar(
                text("INSERT INTO users (telegram_id) VALUES (1000001) RETURNING id")
            )
            await connection.execute(
                text(
                    "INSERT INTO subscription_settings (user_id, subscribe_all) "
                    "VALUES (:user_id, false)"
                ),
                {"user_id": user_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO subscription_categories (user_id, category) "
                    "VALUES (:user_id, 'ai_data')"
                ),
                {"user_id": user_id},
            )

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "UPDATE subscription_settings SET subscribe_all = true "
                        "WHERE user_id = :user_id"
                    ),
                    {"user_id": user_id},
                )
            await transaction.rollback()

        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM subscription_categories WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            await connection.execute(
                text(
                    "UPDATE subscription_settings SET subscribe_all = true WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO subscription_categories (user_id, category) "
                        "VALUES (:user_id, 'other')"
                    ),
                    {"user_id": user_id},
                )
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_localization_category_and_delivery_uniqueness(
    migrated_database_url: str,
) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.begin() as connection:
            user_id = await connection.scalar(
                text("INSERT INTO users (telegram_id) VALUES (1000002) RETURNING id")
            )
            event_id = await connection.scalar(
                text(
                    "INSERT INTO events (source_id, source_theme, starts_at) "
                    "VALUES (2000001, 'it', :starts_at) RETURNING id"
                ),
                {"starts_at": datetime.now(UTC)},
            )
            await connection.execute(
                text(
                    "INSERT INTO event_localizations (event_id, locale, title, content_hash) "
                    "VALUES (:event_id, 'ru', 'Title', 'hash')"
                ),
                {"event_id": event_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO event_categories (event_id, category) "
                    "VALUES (:event_id, 'development_it')"
                ),
                {"event_id": event_id},
            )
            change_id = await connection.scalar(
                text(
                    "INSERT INTO domain_changes "
                    "(event_id, event_revision, notification_type) "
                    "VALUES (:event_id, 1, 'new_event') RETURNING id"
                ),
                {"event_id": event_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO notification_deliveries "
                    "(user_id, domain_change_id, event_id, event_revision, notification_type) "
                    "VALUES (:user_id, :change_id, :event_id, 1, 'new_event')"
                ),
                {"user_id": user_id, "change_id": change_id, "event_id": event_id},
            )

        duplicate_statements = (
            (
                "INSERT INTO event_localizations (event_id, locale, title, content_hash) "
                "VALUES (:event_id, 'ru', 'Duplicate', 'hash-2')",
                {"event_id": event_id},
            ),
            (
                "INSERT INTO event_categories (event_id, category) "
                "VALUES (:event_id, 'development_it')",
                {"event_id": event_id},
            ),
            (
                "INSERT INTO notification_deliveries "
                "(user_id, domain_change_id, event_id, event_revision, notification_type) "
                "VALUES (:user_id, :change_id, :event_id, 1, 'new_event')",
                {"user_id": user_id, "change_id": change_id, "event_id": event_id},
            ),
        )
        for statement, parameters in duplicate_statements:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                with pytest.raises(IntegrityError):
                    await connection.execute(text(statement), parameters)
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_localized_details_url_migration_preserves_existing_value(
    migrated_database_url: str,
    alembic_runner: Callable[..., None],
) -> None:
    await asyncio.to_thread(alembic_runner, command.downgrade, "0005_operations")

    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.begin() as connection:
            event_id = await connection.scalar(
                text(
                    "INSERT INTO events (source_id, source_theme, starts_at, details_url) "
                    "VALUES (2000006, 'it', :starts_at, 'https://example.test/event') "
                    "RETURNING id"
                ),
                {"starts_at": datetime.now(UTC)},
            )
            await connection.execute(
                text(
                    "INSERT INTO event_localizations (event_id, locale, title, content_hash) "
                    "VALUES (:event_id, 'ru', 'RU', 'ru-hash'), "
                    "(:event_id, 'kz', 'KZ', 'kz-hash')"
                ),
                {"event_id": event_id},
            )
    finally:
        await engine.dispose()

    try:
        await asyncio.to_thread(alembic_runner, command.upgrade, "head")
        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.connect() as connection:
                event_columns = await connection.run_sync(
                    lambda sync: {item["name"] for item in inspect(sync).get_columns("events")}
                )
                localization_columns = await connection.run_sync(
                    lambda sync: {
                        item["name"] for item in inspect(sync).get_columns("event_localizations")
                    }
                )
                rows = await connection.execute(
                    text(
                        "SELECT locale::text, details_url FROM event_localizations "
                        "WHERE event_id = :event_id"
                    ),
                    {"event_id": event_id},
                )

            assert "details_url" not in event_columns
            assert "details_url" in localization_columns
            assert dict(rows.all()) == {
                "ru": "https://example.test/event",
                "kz": "https://example.test/event",
            }
        finally:
            await engine.dispose()
    finally:
        await asyncio.to_thread(alembic_runner, command.upgrade, "head")


@pytest.mark.asyncio
async def test_postgresql_enums_round_trip_as_domain_enum_members(
    migrated_database_url: str,
) -> None:
    engine = create_async_engine(migrated_database_url)
    try:
        async with engine.begin() as connection:
            event_id = await connection.scalar(
                text(
                    "INSERT INTO events (source_id, source_theme, starts_at, event_format, state) "
                    "VALUES (2000007, 'marketing', :starts_at, 'hybrid', 'active') RETURNING id"
                ),
                {"starts_at": datetime.now(UTC)},
            )
        async_session = async_sessionmaker(engine, expire_on_commit=False)
        async with async_session() as session:
            event = await session.get(Event, event_id)

        assert event is not None
        assert event.source_theme is SourceTheme.MARKETING
        assert event.event_format is EventFormat.HYBRID
        assert event.state is EventState.ACTIVE
    finally:
        await engine.dispose()
