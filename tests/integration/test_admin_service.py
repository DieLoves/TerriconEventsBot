from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.admin import AdminService
from terricon_events_bot.domain.enums import (
    ClassificationStatus,
    Locale,
    OpenAIOperation,
    SourceTheme,
)
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import (
    Event,
    EventClassification,
    OpenAIUsage,
    User,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
async def admin_database(
    migrated_database_url: str,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users, events RESTART IDENTITY CASCADE"))
        await connection.execute(text("TRUNCATE TABLE openai_usage RESTART IDENTITY CASCADE"))
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE users, events, openai_usage RESTART IDENTITY CASCADE")
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_stats_and_manual_reclassification_reset_queue_state(
    admin_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = admin_database
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            session.add_all(
                [
                    User(
                        telegram_id=7_300_001,
                        locale=Locale.RU,
                        is_active=True,
                        access_granted=True,
                    ),
                    User(
                        telegram_id=7_300_002,
                        locale=Locale.KZ,
                        is_active=False,
                        access_granted=True,
                    ),
                ]
            )
            event = Event(
                source_id=7_300_100,
                source_theme=SourceTheme.IT,
                starts_at=now,
                semantic_hash="a" * 64,
                revision=3,
            )
            session.add(event)
            await session.flush()
            session.add(
                EventClassification(
                    event_id=event.id,
                    status=ClassificationStatus.COMPLETED,
                    semantic_hash="b" * 64,
                    model="old-model",
                    prompt_version="old",
                    attempts=2,
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.5"),
                    result_metadata={},
                )
            )
            session.add(
                OpenAIUsage(
                    event_id=event.id,
                    operation=OpenAIOperation.CLASSIFICATION,
                    model="model",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("1.25"),
                    occurred_at=now,
                )
            )
            event_id = event.id
    service = AdminService(session_factory)

    stats = await service.stats()
    await service.reclassify(event_id)

    async with session_factory() as session:
        classification = await session.get(EventClassification, event_id)
    assert stats.users == 2
    assert stats.active_users == 1
    assert stats.events == 1
    assert stats.openai_cost_usd == Decimal("1.250000000")
    assert classification is not None
    assert classification.status is ClassificationStatus.PENDING
    assert classification.semantic_hash == "a" * 64
    assert classification.attempts == 0
    assert classification.result_metadata["manual_reclassify"] is True
    assert classification.result_metadata["notify_new"] is False
