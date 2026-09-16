from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from terricon_events_bot.infrastructure import models
from terricon_events_bot.infrastructure.database import Base

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

        assert set(Base.metadata.tables) <= tables
        assert {"ix_events_starts_at", "ix_events_available_starts_at"} <= event_indexes
        assert "ix_notification_outbox_pending_available" in outbox_indexes
    finally:
        await engine.dispose()


def test_migrated_schema_matches_sqlalchemy_metadata(migrated_database_url: str) -> None:
    command.check(Config("alembic.ini"))


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
