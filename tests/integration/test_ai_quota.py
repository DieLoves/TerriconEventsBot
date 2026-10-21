from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.domain.ai import AIAdapterError, AIUsageData
from terricon_events_bot.infrastructure.ai_quota import PostgresAIQuota, QuotaLimits
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import AIQuotaReservation

pytestmark = pytest.mark.postgres
NOW = datetime(2030, 1, 1, tzinfo=UTC)


class Clock:
    value = NOW

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
async def quota_db(migrated_database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE ai_quota_states CASCADE"))
    try:
        yield create_session_factory(engine)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE ai_quota_states CASCADE"))
        await engine.dispose()


@pytest.mark.parametrize(
    ("limits", "tokens", "delay"),
    [
        (QuotaLimits(1, 100, 100, 1000), 10, 60),
        (QuotaLimits(100, 100, 10, 1000), 6, 60),
        (QuotaLimits(100, 1, 100, 1000), 10, 86400),
        (QuotaLimits(100, 100, 100, 10), 6, 86400),
    ],
)
async def test_each_window_survives_restart_and_recovers(quota_db, limits, tokens, delay):
    clock = Clock()
    guard = PostgresAIQuota(quota_db, "shared", limits, clock=clock)
    await guard.reserve(tokens)
    restarted = PostgresAIQuota(quota_db, "shared", limits, clock=clock)
    with pytest.raises(AIAdapterError) as caught:
        await restarted.reserve(tokens)
    assert caught.value.code == "rate_limit"
    assert caught.value.retry_after_seconds == delay
    clock.value += timedelta(seconds=delay)
    await restarted.reserve(tokens)


async def test_concurrent_reservations_do_not_oversubscribe(quota_db):
    clock = Clock()
    limits = QuotaLimits(3, 100, 1000, 10000)
    guards = [PostgresAIQuota(quota_db, "shared", limits, clock=clock) for _ in range(12)]
    results = await asyncio.gather(*(g.reserve(10) for g in guards), return_exceptions=True)
    assert sum(not isinstance(x, Exception) for x in results) == 3
    assert all(isinstance(x, AIAdapterError) for x in results if isinstance(x, Exception))
    async with quota_db() as session:
        assert await session.scalar(select(func.count()).select_from(AIQuotaReservation)) == 3


async def test_settle_usage_releases_unused_tokens_but_unknown_outcome_keeps_reserve(quota_db):
    guard = PostgresAIQuota(quota_db, "shared", QuotaLimits(100, 100, 100, 1000), clock=Clock())
    reservation = await guard.reserve(90)
    with pytest.raises(AIAdapterError):
        await guard.reserve(20)
    await guard.settle(reservation, AIUsageData(5, 5), {})
    unknown = await guard.reserve(90)
    await guard.settle(unknown, None, {})
    with pytest.raises(AIAdapterError):
        await guard.reserve(1)


async def test_external_remaining_headers_and_cooldown_survive_restart(quota_db):
    clock = Clock()
    limits = QuotaLimits(100, 100, 1000, 10000)
    guard = PostgresAIQuota(quota_db, "shared", limits, clock=clock)
    reservation = await guard.reserve(10)
    await guard.settle(
        reservation,
        AIUsageData(2, 3),
        {
            "x-ratelimit-remaining-tokens": "4",
            "x-ratelimit-reset-tokens": "1m2.5s",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "2h3m",
        },
        retry_after_seconds=120,
    )
    restarted = PostgresAIQuota(quota_db, "shared", limits, clock=clock)
    with pytest.raises(AIAdapterError) as caught:
        await restarted.reserve(10)
    assert caught.value.retry_after_seconds == 7380
    clock.value += timedelta(seconds=7380)
    await restarted.reserve(10)


async def test_stale_headers_never_increase_remaining_and_bad_headers_are_ignored(quota_db):
    clock = Clock()
    guard = PostgresAIQuota(quota_db, "shared", QuotaLimits(100, 100, 1000, 10000), clock=clock)
    first = await guard.reserve(10)
    second = await guard.reserve(10)
    await guard.settle(
        second,
        None,
        {
            "x-ratelimit-remaining-tokens": "5",
            "x-ratelimit-reset-tokens": "30s",
        },
    )
    await guard.settle(
        first,
        None,
        {
            "x-ratelimit-remaining-tokens": "999",
            "x-ratelimit-reset-tokens": "30s",
            "x-ratelimit-remaining-requests": "nan",
            "x-ratelimit-reset-requests": "inf",
        },
    )
    with pytest.raises(AIAdapterError) as caught:
        await guard.reserve(6)
    assert caught.value.retry_after_seconds == 30


async def test_oversized_request_never_consumes_quota(quota_db):
    guard = PostgresAIQuota(quota_db, "shared", QuotaLimits(1, 1, 10, 100), clock=Clock())
    with pytest.raises(AIAdapterError, match="quota_request_too_large"):
        await guard.reserve(11)
    await guard.reserve(10)


@pytest.mark.parametrize(
    ("kind", "remaining", "reset", "required", "delay"),
    [
        ("tokens", "25", "30s", 20, 30),
        ("requests", "1", "2h", 1, 7200),
    ],
)
async def test_response_headers_reserve_other_inflight_requests(
    quota_db,
    kind,
    remaining,
    reset,
    required,
    delay,
):
    guard = PostgresAIQuota(quota_db, "inflight", QuotaLimits(100, 100, 1000, 10000), clock=Clock())
    first = await guard.reserve(10)
    await guard.reserve(20)
    await guard.settle(
        first,
        AIUsageData(2, 3),
        {
            f"x-ratelimit-remaining-{kind}": remaining,
            f"x-ratelimit-reset-{kind}": reset,
        },
    )
    with pytest.raises(AIAdapterError) as caught:
        await guard.reserve(required)
    assert caught.value.retry_after_seconds == delay
