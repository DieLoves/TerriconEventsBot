from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.domain.ai import AIAdapterError, AIUsageData
from terricon_events_bot.infrastructure.models.ai_quota import AIQuotaReservation, AIQuotaState

_MINUTE = timedelta(minutes=1)
_DAY = timedelta(days=1)
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?")


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    rpm: int = 24
    rpd: int = 800
    tpm: int = 6400
    tpd: int = 160000

    def __post_init__(self) -> None:
        if any(type(x) is not int or x <= 0 for x in (self.rpm, self.rpd, self.tpm, self.tpd)):
            raise ValueError("Quota limits must be positive integers")


def _reset_delay(value: str) -> float | None:
    if len(value) > 64:
        return None
    match = _DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    delay = sum(float(x or 0) * unit for x, unit in zip(match.groups(), (3600, 60, 1), strict=True))
    return min(delay, 7 * 86400) if math.isfinite(delay) and delay > 0 else None


class AIQuota(Protocol):
    async def reserve(self, tokens: int) -> UUID: ...

    async def settle(
        self,
        reservation_id: UUID,
        usage: AIUsageData | None,
        headers: Mapping[str, str],
        *,
        retry_after_seconds: float | None = None,
    ) -> None: ...


class PostgresAIQuota:
    """Rolling local quotas, shared by every operation using the same DB and scope.

    Reservations are committed BEFORE HTTP; a crash/unknown usage retains their
    full estimate. No DB transaction or connection is held during network I/O.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        scope: str,
        limits: QuotaLimits,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not scope.strip() or len(scope) > 128:
            raise ValueError("Quota scope must contain 1-128 characters")
        self._session_factory = session_factory
        self._scope = scope
        self._limits = limits
        self._clock = clock

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Quota clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    async def _state(self, session: AsyncSession) -> AIQuotaState:
        await session.execute(
            pg_insert(AIQuotaState).values(scope=self._scope).on_conflict_do_nothing()
        )
        state = await session.scalar(
            select(AIQuotaState).where(AIQuotaState.scope == self._scope).with_for_update()
        )
        assert state is not None  # DB row was inserted or already exists. # noqa: S101
        return state

    async def reserve(self, tokens: int) -> UUID:
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("Reserved tokens must be a positive integer")
        if tokens > min(self._limits.tpm, self._limits.tpd):
            raise AIAdapterError("quota_request_too_large")
        async with self._session_factory() as session, session.begin():
            state = await self._state(session)
            # Read after acquiring the serialization lock so waiting never ages a reservation.
            now = self._now()
            await session.execute(
                delete(AIQuotaReservation).where(
                    AIQuotaReservation.scope == self._scope,
                    AIQuotaReservation.created_at <= now - _DAY,
                )
            )
            rows = (
                await session.scalars(
                    select(AIQuotaReservation)
                    .where(
                        AIQuotaReservation.scope == self._scope,
                        AIQuotaReservation.created_at > now - _DAY,
                    )
                    .order_by(AIQuotaReservation.created_at)
                )
            ).all()
            deadlines = (
                [state.blocked_until] if state.blocked_until and state.blocked_until > now else []
            )
            for window, request_limit, token_limit in (
                (_MINUTE, self._limits.rpm, self._limits.tpm),
                (_DAY, self._limits.rpd, self._limits.tpd),
            ):
                active = [r for r in rows if r.created_at > now - window]
                requests, used = len(active) + 1, sum(r.tokens for r in active) + tokens
                for row in active:
                    if requests <= request_limit and used <= token_limit:
                        break
                    deadlines.append(row.created_at + window)
                    requests -= 1
                    used -= row.tokens
            for remaining_name, reset_name, required in (
                ("remaining_requests", "requests_reset_at", 1),
                ("remaining_tokens", "tokens_reset_at", tokens),
            ):
                reset_at = getattr(state, reset_name)
                remaining = getattr(state, remaining_name)
                if reset_at and reset_at > now and remaining is not None and remaining < required:
                    deadlines.append(reset_at)
            if deadlines:
                raise AIAdapterError(
                    "rate_limit", retry_after_seconds=(max(deadlines) - now).total_seconds()
                )
            for remaining_name, reset_name, required in (
                ("remaining_requests", "requests_reset_at", 1),
                ("remaining_tokens", "tokens_reset_at", tokens),
            ):
                reset_at = getattr(state, reset_name)
                remaining = getattr(state, remaining_name)
                if reset_at and reset_at > now and remaining is not None:
                    setattr(state, remaining_name, remaining - required)
            reservation_id = uuid4()
            session.add(
                AIQuotaReservation(
                    id=reservation_id, scope=self._scope, tokens=tokens, created_at=now
                )
            )
            return reservation_id

    async def settle(
        self,
        reservation_id: UUID,
        usage: AIUsageData | None,
        headers: Mapping[str, str],
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            state = await self._state(session)
            now = self._now()
            row = await session.get(AIQuotaReservation, reservation_id)
            if row is not None and row.scope == self._scope:
                row.settled = True
                if usage is not None:
                    row.tokens = usage.input_tokens + usage.output_tokens
            outstanding = (
                await session.scalars(
                    select(AIQuotaReservation).where(
                        AIQuotaReservation.scope == self._scope,
                        AIQuotaReservation.id != reservation_id,
                        AIQuotaReservation.settled.is_(False),
                        AIQuotaReservation.created_at > now - _DAY,
                    )
                )
            ).all()
            if (
                retry_after_seconds is not None
                and math.isfinite(retry_after_seconds)
                and retry_after_seconds > 0
            ):
                deadline = now + timedelta(seconds=min(retry_after_seconds, 7 * 86400))
                state.blocked_until = max(state.blocked_until or now, deadline)
            # Header values are minima: delayed concurrent responses cannot replenish quota.
            for kind in ("requests", "tokens"):
                raw_remaining = headers.get(f"x-ratelimit-remaining-{kind}", "")
                delay = _reset_delay(headers.get(f"x-ratelimit-reset-{kind}", ""))
                if (
                    not raw_remaining.isascii()
                    or not raw_remaining.isdigit()
                    or len(raw_remaining) > 12
                    or delay is None
                ):
                    continue
                # Response A may precede concurrent request B at the provider.
                # Keep B's full reservation even if the header already counted it.
                debt = (
                    len(outstanding)
                    if kind == "requests"
                    else sum(item.tokens for item in outstanding if item.created_at > now - _MINUTE)
                )
                remaining = max(0, int(raw_remaining) - debt)
                reset_at = now + timedelta(seconds=delay)
                old_reset = getattr(state, f"{kind}_reset_at")
                old_remaining = getattr(state, f"remaining_{kind}")
                if old_reset and old_reset > now and old_remaining is not None:
                    remaining = min(remaining, old_remaining)
                    reset_at = max(reset_at, old_reset)
                setattr(state, f"remaining_{kind}", remaining)
                setattr(state, f"{kind}_reset_at", reset_at)
