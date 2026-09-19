from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from terricon_events_bot.application.delivery import TelegramSendError
from terricon_events_bot.domain.enums import (
    BroadcastAudience,
    BroadcastStatus,
    DeliveryStatus,
    Locale,
)
from terricon_events_bot.infrastructure.models import (
    Broadcast,
    BroadcastDelivery,
    BroadcastLocalization,
    NotificationDelivery,
    NotificationOutbox,
    User,
)

_TEXT_CHUNK_LIMIT = 4000
_BODY_LIMIT = 16000
_FILE_ID_LIMIT = 255
_LEASE = timedelta(minutes=5)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class BroadcastGateway(Protocol):
    async def send_message(self, telegram_id: int, text: str) -> int: ...

    async def send_photo(self, telegram_id: int, telegram_file_id: str) -> int: ...

    async def send_report(self, admin_telegram_id: int, text: str) -> int: ...


class BroadcastError(ValueError):
    pass


class BroadcastStateError(BroadcastError):
    pass


@dataclass(frozen=True, slots=True)
class BroadcastDraft:
    broadcast_id: int
    created_by_telegram_id: int
    audience: BroadcastAudience
    telegram_file_id: str | None
    bodies: dict[Locale, str]
    status: BroadcastStatus


@dataclass(frozen=True, slots=True)
class BroadcastReport:
    broadcast_id: int
    status: BroadcastStatus
    total: int
    sent: int
    transient: int
    failed: int
    blocked: int


@dataclass(frozen=True, slots=True)
class BroadcastBatchResult:
    claimed: int
    sent: int
    retrying: int
    failed: int
    blocked: int


def split_broadcast_text(body: str, limit: int = _TEXT_CHUNK_LIMIT) -> tuple[str, ...]:
    if limit <= 0 or limit > 4096:
        raise ValueError("Broadcast chunk limit is invalid")
    normalized = body.strip()
    if not normalized:
        raise BroadcastError("Broadcast text must not be empty")
    chunks: list[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk or remaining[:limit])
        remaining = remaining[split_at:].lstrip()
    return tuple(chunks)


class BroadcastService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def create_draft(
        self, admin_telegram_id: int, audience: BroadcastAudience
    ) -> BroadcastDraft:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(Broadcast)
                    .where(
                        Broadcast.created_by_telegram_id == admin_telegram_id,
                        Broadcast.status == BroadcastStatus.DRAFT,
                    )
                    .values(status=BroadcastStatus.CANCELLED, updated_at=now)
                )
                broadcast = Broadcast(
                    created_by_telegram_id=admin_telegram_id,
                    audience=audience,
                    status=BroadcastStatus.DRAFT,
                    created_at=now,
                    updated_at=now,
                )
                session.add(broadcast)
                await session.flush()
                return BroadcastDraft(
                    broadcast.id,
                    admin_telegram_id,
                    audience,
                    None,
                    {},
                    BroadcastStatus.DRAFT,
                )

    async def set_body(self, broadcast_id: int, locale: Locale, body: str) -> BroadcastDraft:
        normalized = self._normalize_body(body)
        async with self._session_factory() as session:
            async with session.begin():
                broadcast = await self._draft_for_update(session, broadcast_id)
                if locale not in self._required_locales(broadcast.audience):
                    raise BroadcastError("Locale does not belong to broadcast audience")
                await session.execute(
                    pg_insert(BroadcastLocalization)
                    .values(
                        broadcast_id=broadcast_id,
                        locale=locale,
                        body=normalized,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            BroadcastLocalization.broadcast_id,
                            BroadcastLocalization.locale,
                        ],
                        set_={"body": normalized},
                    )
                )
                return await self._draft_view(session, broadcast)

    async def set_photo(self, broadcast_id: int, telegram_file_id: str | None) -> BroadcastDraft:
        file_id = self._normalize_file_id(telegram_file_id)
        async with self._session_factory() as session:
            async with session.begin():
                broadcast = await self._draft_for_update(session, broadcast_id)
                broadcast.telegram_file_id = file_id
                return await self._draft_view(session, broadcast)

    async def draft(self, broadcast_id: int) -> BroadcastDraft | None:
        async with self._session_factory() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            return await self._draft_view(session, broadcast) if broadcast is not None else None

    async def active_draft(self, admin_telegram_id: int) -> BroadcastDraft | None:
        async with self._session_factory() as session:
            broadcast = await session.scalar(
                select(Broadcast)
                .where(
                    Broadcast.created_by_telegram_id == admin_telegram_id,
                    Broadcast.status == BroadcastStatus.DRAFT,
                )
                .order_by(Broadcast.id.desc())
                .limit(1)
            )
            return await self._draft_view(session, broadcast) if broadcast is not None else None

    async def confirm(self, broadcast_id: int) -> BroadcastReport:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                broadcast = await self._draft_for_update(session, broadcast_id)
                draft = await self._draft_view(session, broadcast)
                missing = self._required_locales(broadcast.audience) - set(draft.bodies)
                if missing:
                    raise BroadcastStateError("Broadcast localizations are incomplete")
                conditions: list[ColumnElement[bool]] = [
                    User.is_active.is_(True),
                    User.access_granted.is_(True),
                ]
                if broadcast.audience is BroadcastAudience.RU:
                    conditions.append(User.locale == Locale.RU)
                elif broadcast.audience is BroadcastAudience.KZ:
                    conditions.append(User.locale == Locale.KZ)
                user_ids = tuple(
                    (
                        await session.scalars(select(User.id).where(*conditions).order_by(User.id))
                    ).all()
                )
                if user_ids:
                    await session.execute(
                        pg_insert(BroadcastDelivery)
                        .values(
                            [
                                {
                                    "broadcast_id": broadcast_id,
                                    "user_id": user_id,
                                    "status": DeliveryStatus.PENDING,
                                    "next_attempt_at": now,
                                }
                                for user_id in user_ids
                            ]
                        )
                        .on_conflict_do_nothing(
                            constraint="uq_broadcast_deliveries_broadcast_id_user_id"
                        )
                    )
                broadcast.total_count = len(user_ids)
                broadcast.confirmed_at = now
                broadcast.status = BroadcastStatus.READY if user_ids else BroadcastStatus.COMPLETED
                if not user_ids:
                    broadcast.completed_at = now
                return BroadcastReport(
                    broadcast_id,
                    broadcast.status,
                    len(user_ids),
                    0,
                    len(user_ids),
                    0,
                    0,
                )

    async def cancel(self, broadcast_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                broadcast = await self._draft_for_update(session, broadcast_id)
                broadcast.status = BroadcastStatus.CANCELLED

    async def report(self, broadcast_id: int) -> BroadcastReport | None:
        async with self._session_factory() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return None
            counts = await self._delivery_counts(session, broadcast_id)
            return self._report(broadcast, counts)

    async def _draft_for_update(self, session: AsyncSession, broadcast_id: int) -> Broadcast:
        broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
        if broadcast is None:
            raise LookupError("Broadcast does not exist")
        if broadcast.status is not BroadcastStatus.DRAFT:
            raise BroadcastStateError("Broadcast is not a draft")
        return broadcast

    @staticmethod
    async def _draft_view(session: AsyncSession, broadcast: Broadcast) -> BroadcastDraft:
        rows = (
            await session.scalars(
                select(BroadcastLocalization).where(
                    BroadcastLocalization.broadcast_id == broadcast.id
                )
            )
        ).all()
        return BroadcastDraft(
            broadcast.id,
            broadcast.created_by_telegram_id,
            broadcast.audience,
            broadcast.telegram_file_id,
            {row.locale: row.body for row in rows},
            broadcast.status,
        )

    @staticmethod
    def _required_locales(audience: BroadcastAudience) -> set[Locale]:
        if audience is BroadcastAudience.RU:
            return {Locale.RU}
        if audience is BroadcastAudience.KZ:
            return {Locale.KZ}
        return {Locale.RU, Locale.KZ}

    @staticmethod
    def _normalize_body(body: str) -> str:
        normalized = body.strip()
        if not normalized or len(normalized) > _BODY_LIMIT:
            raise BroadcastError("Broadcast text is invalid")
        return normalized

    @staticmethod
    def _normalize_file_id(file_id: str | None) -> str | None:
        if file_id is None:
            return None
        normalized = file_id.strip()
        if not normalized or len(normalized) > _FILE_ID_LIMIT:
            raise BroadcastError("Broadcast Telegram file ID is invalid")
        return normalized

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Broadcast clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    async def _delivery_counts(
        session: AsyncSession, broadcast_id: int
    ) -> dict[DeliveryStatus, int]:
        rows = (
            await session.execute(
                select(BroadcastDelivery.status, func.count())
                .where(BroadcastDelivery.broadcast_id == broadcast_id)
                .group_by(BroadcastDelivery.status)
            )
        ).all()
        return {status: count for status, count in rows}

    @staticmethod
    def _report(broadcast: Broadcast, counts: dict[DeliveryStatus, int]) -> BroadcastReport:
        return BroadcastReport(
            broadcast.id,
            broadcast.status,
            broadcast.total_count,
            counts.get(DeliveryStatus.SENT, 0),
            sum(
                counts.get(status, 0)
                for status in (
                    DeliveryStatus.PENDING,
                    DeliveryStatus.PROCESSING,
                    DeliveryStatus.RETRY_WAIT,
                )
            ),
            counts.get(DeliveryStatus.FAILED, 0),
            counts.get(DeliveryStatus.BLOCKED, 0),
        )


class BroadcastWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: BroadcastGateway,
        *,
        clock: Clock | None = None,
        lease: timedelta = _LEASE,
    ) -> None:
        if lease <= timedelta():
            raise ValueError("Broadcast lease must be positive")
        self._session_factory = session_factory
        self._gateway = gateway
        self._clock = clock or SystemClock()
        self._lease = lease

    async def run_batch(self, *, limit: int = 20) -> BroadcastBatchResult:
        if limit <= 0:
            raise ValueError("Broadcast batch limit must be positive")
        ids = await self._claim(limit)
        counts: defaultdict[str, int] = defaultdict(int)
        for delivery_id in ids:
            counts[await self._deliver(delivery_id)] += 1
        await self._send_completed_reports()
        return BroadcastBatchResult(
            len(ids),
            counts["sent"],
            counts["retrying"],
            counts["failed"],
            counts["blocked"],
        )

    async def _claim(self, limit: int) -> tuple[int, ...]:
        now = self._now()
        stale = now - self._lease
        async with self._session_factory() as session:
            async with session.begin():
                rows = list(
                    (
                        await session.scalars(
                            select(BroadcastDelivery)
                            .join(Broadcast, Broadcast.id == BroadcastDelivery.broadcast_id)
                            .where(
                                Broadcast.status.in_(
                                    (BroadcastStatus.READY, BroadcastStatus.RUNNING)
                                ),
                                or_(
                                    and_(
                                        BroadcastDelivery.status.in_(
                                            (DeliveryStatus.PENDING, DeliveryStatus.RETRY_WAIT)
                                        ),
                                        or_(
                                            BroadcastDelivery.next_attempt_at.is_(None),
                                            BroadcastDelivery.next_attempt_at <= now,
                                        ),
                                    ),
                                    and_(
                                        BroadcastDelivery.status == DeliveryStatus.PROCESSING,
                                        or_(
                                            BroadcastDelivery.locked_at.is_(None),
                                            BroadcastDelivery.locked_at <= stale,
                                        ),
                                    ),
                                ),
                            )
                            .order_by(BroadcastDelivery.id)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                broadcast_ids = {row.broadcast_id for row in rows}
                for row in rows:
                    row.status = DeliveryStatus.PROCESSING
                    row.locked_at = now
                    row.attempts += 1
                if broadcast_ids:
                    await session.execute(
                        update(Broadcast)
                        .where(
                            Broadcast.id.in_(broadcast_ids),
                            Broadcast.status == BroadcastStatus.READY,
                        )
                        .values(status=BroadcastStatus.RUNNING, started_at=now)
                    )
                return tuple(row.id for row in rows)

    async def _deliver(self, delivery_id: int) -> str:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(BroadcastDelivery, Broadcast, User)
                    .join(Broadcast, Broadcast.id == BroadcastDelivery.broadcast_id)
                    .join(User, User.id == BroadcastDelivery.user_id)
                    .where(BroadcastDelivery.id == delivery_id)
                )
            ).one_or_none()
            if row is None:
                return "failed"
            delivery, broadcast, user = row
            if delivery.status is not DeliveryStatus.PROCESSING:
                return "failed"
            if not user.is_active or not user.access_granted:
                await self._block(delivery_id, user.id, "inactive_user")
                return "blocked"
            body = await session.scalar(
                select(BroadcastLocalization.body).where(
                    BroadcastLocalization.broadcast_id == broadcast.id,
                    BroadcastLocalization.locale == user.locale,
                )
            )
            if body is None:
                await self._fail(delivery_id, "missing_localization")
                return "failed"
            actions: list[tuple[str, str]] = []
            if broadcast.telegram_file_id is not None:
                actions.append(("photo", broadcast.telegram_file_id))
            actions.extend(("text", chunk) for chunk in split_broadcast_text(body))
            acknowledged = len(delivery.telegram_message_ids)
            telegram_id = user.telegram_id
        for kind, value in actions[acknowledged:]:
            try:
                message_id = (
                    await self._gateway.send_photo(telegram_id, value)
                    if kind == "photo"
                    else await self._gateway.send_message(telegram_id, value)
                )
            except TelegramSendError as error:
                if error.blocked:
                    await self._block(delivery_id, user.id, error.code)
                    return "blocked"
                if error.transient:
                    await self._retry(delivery_id, error)
                    return "retrying"
                await self._fail(delivery_id, error.code)
                return "failed"
            await self._ack(delivery_id, message_id)
        await self._complete(delivery_id)
        return "sent"

    async def _ack(self, delivery_id: int, message_id: int) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                delivery = await session.get(BroadcastDelivery, delivery_id, with_for_update=True)
                if delivery is None or delivery.status is not DeliveryStatus.PROCESSING:
                    raise RuntimeError("Broadcast delivery is no longer processing")
                delivery.telegram_message_ids = [
                    *delivery.telegram_message_ids,
                    message_id,
                ]
                delivery.telegram_message_id = message_id
                delivery.locked_at = now

    async def _complete(self, delivery_id: int) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                delivery = await session.get(BroadcastDelivery, delivery_id, with_for_update=True)
                if delivery is None:
                    return
                delivery.status = DeliveryStatus.SENT
                delivery.sent_at = now
                delivery.locked_at = None
                delivery.next_attempt_at = None
                delivery.last_error_code = None
                await self._refresh_broadcast(session, delivery.broadcast_id, now)

    async def _retry(self, delivery_id: int, error: TelegramSendError) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                delivery = await session.get(BroadcastDelivery, delivery_id, with_for_update=True)
                if delivery is None:
                    return
                backoff = timedelta(seconds=min(30 * (2 ** min(delivery.attempts - 1, 7)), 3600))
                delivery.status = DeliveryStatus.RETRY_WAIT
                delivery.next_attempt_at = now + max(backoff, error.retry_after or timedelta())
                delivery.locked_at = None
                delivery.last_error_code = error.code
                await self._refresh_broadcast(session, delivery.broadcast_id, now)

    async def _fail(self, delivery_id: int, code: str) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                delivery = await session.get(BroadcastDelivery, delivery_id, with_for_update=True)
                if delivery is None:
                    return
                delivery.status = DeliveryStatus.FAILED
                delivery.locked_at = None
                delivery.last_error_code = code[:128]
                await self._refresh_broadcast(session, delivery.broadcast_id, now)

    async def _block(self, delivery_id: int, user_id: int, code: str) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                delivery = await session.get(BroadcastDelivery, delivery_id, with_for_update=True)
                if delivery is None:
                    return
                broadcast_ids = set(
                    (
                        await session.scalars(
                            select(BroadcastDelivery.broadcast_id).where(
                                BroadcastDelivery.user_id == user_id,
                                BroadcastDelivery.status.in_(
                                    (
                                        DeliveryStatus.PENDING,
                                        DeliveryStatus.PROCESSING,
                                        DeliveryStatus.RETRY_WAIT,
                                    )
                                ),
                            )
                        )
                    ).all()
                )
                await session.execute(
                    update(BroadcastDelivery)
                    .where(
                        BroadcastDelivery.user_id == user_id,
                        BroadcastDelivery.status.in_(
                            (
                                DeliveryStatus.PENDING,
                                DeliveryStatus.PROCESSING,
                                DeliveryStatus.RETRY_WAIT,
                            )
                        ),
                    )
                    .values(
                        status=DeliveryStatus.BLOCKED,
                        locked_at=None,
                        last_error_code=code[:128],
                    )
                )
                await session.execute(
                    update(User).where(User.id == user_id).values(is_active=False)
                )
                await session.execute(
                    update(NotificationOutbox)
                    .where(
                        NotificationOutbox.user_id == user_id,
                        NotificationOutbox.status.in_(
                            (
                                DeliveryStatus.PENDING,
                                DeliveryStatus.PROCESSING,
                                DeliveryStatus.RETRY_WAIT,
                            )
                        ),
                    )
                    .values(status=DeliveryStatus.BLOCKED, last_error_code=code[:128])
                )
                await session.execute(
                    update(NotificationDelivery)
                    .where(
                        NotificationDelivery.user_id == user_id,
                        NotificationDelivery.status.in_(
                            (
                                DeliveryStatus.PENDING,
                                DeliveryStatus.PROCESSING,
                                DeliveryStatus.RETRY_WAIT,
                            )
                        ),
                    )
                    .values(status=DeliveryStatus.BLOCKED, last_error_code=code[:128])
                )
                for broadcast_id in sorted(broadcast_ids):
                    await self._refresh_broadcast(session, broadcast_id, now)

    async def _send_completed_reports(self) -> None:
        async with self._session_factory() as session:
            broadcast_ids = tuple(
                (
                    await session.scalars(
                        select(Broadcast.id)
                        .where(
                            Broadcast.status == BroadcastStatus.COMPLETED,
                            Broadcast.report_sent_at.is_(None),
                        )
                        .order_by(Broadcast.id)
                    )
                ).all()
            )
        for broadcast_id in broadcast_ids:
            try:
                await self._send_completed_report(broadcast_id)
            except TelegramSendError:
                continue

    async def _send_completed_report(self, broadcast_id: int) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
                if (
                    broadcast is None
                    or broadcast.status is not BroadcastStatus.COMPLETED
                    or broadcast.report_sent_at is not None
                ):
                    return
                counts = await BroadcastService._delivery_counts(session, broadcast_id)
                report = BroadcastService._report(broadcast, counts)
                await self._gateway.send_report(
                    broadcast.created_by_telegram_id,
                    (
                        f"Рассылка #{broadcast.id} завершена. "
                        f"Доставлено: {report.sent}; ошибок: {report.failed}; "
                        f"бот заблокирован: {report.blocked}."
                    ),
                )
                broadcast.report_sent_at = now

    async def _refresh_broadcast(
        self, session: AsyncSession, broadcast_id: int, now: datetime
    ) -> None:
        broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
        if broadcast is None:
            return
        await session.flush()
        counts = await BroadcastService._delivery_counts(session, broadcast_id)
        broadcast.sent_count = counts.get(DeliveryStatus.SENT, 0)
        broadcast.failed_count = counts.get(DeliveryStatus.FAILED, 0) + counts.get(
            DeliveryStatus.BLOCKED, 0
        )
        unfinished = sum(
            counts.get(status, 0)
            for status in (
                DeliveryStatus.PENDING,
                DeliveryStatus.PROCESSING,
                DeliveryStatus.RETRY_WAIT,
            )
        )
        if unfinished == 0:
            broadcast.status = BroadcastStatus.COMPLETED
            broadcast.completed_at = now

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Broadcast clock must return a timezone-aware datetime")
        return now.astimezone(UTC)
