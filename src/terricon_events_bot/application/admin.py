from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.domain.enums import (
    BroadcastStatus,
    ClassificationStatus,
    DeliveryStatus,
    TicketStatus,
)
from terricon_events_bot.infrastructure.models import (
    Broadcast,
    BroadcastDelivery,
    Event,
    EventClassification,
    FeedbackTicket,
    NotificationOutbox,
    OpenAIUsage,
    SubscriptionCategory,
    SubscriptionSettings,
    SyncEndpointState,
    SyncState,
    User,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AdminStatus:
    baseline_completed_at: datetime | None
    last_full_success_at: datetime | None
    endpoint_failures: int
    pending_classifications: int
    pending_outbox: int
    unfinished_broadcasts: int


@dataclass(frozen=True, slots=True)
class AdminStats:
    users: int
    active_users: int
    subscribe_all: int
    category_subscriptions: int
    events: int
    open_tickets: int
    broadcasts: int
    openai_cost_usd: Decimal


class AdminService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def status(self) -> AdminStatus:
        async with self._session_factory() as session:
            sync = await session.get(SyncState, 1)
            endpoint_failures = await session.scalar(
                select(func.count())
                .select_from(SyncEndpointState)
                .where(SyncEndpointState.consecutive_failures > 0)
            )
            pending_classifications = await session.scalar(
                select(func.count())
                .select_from(EventClassification)
                .where(
                    EventClassification.status.in_(
                        (ClassificationStatus.PENDING, ClassificationStatus.RETRY_WAIT)
                    )
                )
            )
            pending_outbox = await session.scalar(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(
                    NotificationOutbox.status.in_(
                        (
                            DeliveryStatus.PENDING,
                            DeliveryStatus.PROCESSING,
                            DeliveryStatus.RETRY_WAIT,
                        )
                    )
                )
            )
            unfinished_broadcasts = await session.scalar(
                select(func.count())
                .select_from(Broadcast)
                .where(Broadcast.status.in_((BroadcastStatus.READY, BroadcastStatus.RUNNING)))
            )
        return AdminStatus(
            sync.baseline_completed_at if sync else None,
            sync.last_full_success_at if sync else None,
            endpoint_failures or 0,
            pending_classifications or 0,
            pending_outbox or 0,
            unfinished_broadcasts or 0,
        )

    async def stats(self) -> AdminStats:
        now = self._now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self._session_factory() as session:
            users = await session.scalar(select(func.count()).select_from(User))
            active_users = await session.scalar(
                select(func.count()).select_from(User).where(User.is_active.is_(True))
            )
            subscribe_all = await session.scalar(
                select(func.count())
                .select_from(SubscriptionSettings)
                .where(SubscriptionSettings.subscribe_all.is_(True))
            )
            category_subscriptions = await session.scalar(
                select(func.count()).select_from(SubscriptionCategory)
            )
            events = await session.scalar(select(func.count()).select_from(Event))
            open_tickets = await session.scalar(
                select(func.count())
                .select_from(FeedbackTicket)
                .where(FeedbackTicket.status != TicketStatus.CLOSED)
            )
            broadcasts = await session.scalar(select(func.count()).select_from(Broadcast))
            cost = await session.scalar(
                select(func.coalesce(func.sum(OpenAIUsage.cost_usd), 0)).where(
                    OpenAIUsage.occurred_at >= month_start
                )
            )
        return AdminStats(
            users or 0,
            active_users or 0,
            subscribe_all or 0,
            category_subscriptions or 0,
            events or 0,
            open_tickets or 0,
            broadcasts or 0,
            Decimal(cost or 0),
        )

    async def reclassify(self, event_id: int) -> None:
        if event_id <= 0:
            raise ValueError("Event ID must be positive")
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                event = await session.get(Event, event_id, with_for_update=True)
                if event is None:
                    raise LookupError("Event does not exist")
                if event.semantic_hash is None:
                    raise ValueError("Event has no classification input")
                classification = await session.get(
                    EventClassification, event_id, with_for_update=True
                )
                metadata: dict[str, object] = {
                    "notify_new": False,
                    "queued_revision": event.revision,
                    "manual_reclassify": True,
                }
                if classification is None:
                    session.add(
                        EventClassification(
                            event_id=event_id,
                            status=ClassificationStatus.PENDING,
                            semantic_hash=event.semantic_hash,
                            first_attempt_at=now,
                            next_attempt_at=now,
                            result_metadata=metadata,
                            created_at=now,
                        )
                    )
                    return
                classification.status = ClassificationStatus.PENDING
                classification.semantic_hash = event.semantic_hash
                classification.model = None
                classification.prompt_version = None
                classification.attempts = 0
                classification.input_tokens = 0
                classification.output_tokens = 0
                classification.cost_usd = Decimal("0")
                classification.first_attempt_at = now
                classification.next_attempt_at = now
                classification.classified_at = None
                classification.last_error_code = None
                classification.result_metadata = metadata

    async def broadcast_delivery_report(self, broadcast_id: int) -> dict[DeliveryStatus, int]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(BroadcastDelivery.status, func.count())
                    .where(BroadcastDelivery.broadcast_id == broadcast_id)
                    .group_by(BroadcastDelivery.status)
                )
            ).all()
        return {status: count for status, count in rows}

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Admin clock must return a timezone-aware datetime")
        return now.astimezone(UTC)
