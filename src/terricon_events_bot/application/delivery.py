from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from terricon_events_bot.domain.enums import (
    BroadcastStatus,
    CategorySlug,
    ClassificationStatus,
    DeliveryStatus,
    Locale,
    NotificationType,
)
from terricon_events_bot.domain.event_kinds import KIND_ICONS, event_kind, is_program
from terricon_events_bot.infrastructure.models import (
    Broadcast,
    BroadcastDelivery,
    DomainChange,
    Event,
    EventClassification,
    EventLocalization,
    NotificationDelivery,
    NotificationOutbox,
    SubscriptionCategory,
    SubscriptionSettings,
    User,
)
from terricon_events_bot.infrastructure.terricon.normalization import (
    normalize_http_url,
    registration_link,
)
from terricon_events_bot.localization import LocalizationCatalog

_TELEGRAM_TEXT_LIMIT = 4096
_SAFE_TEXT_LIMIT = 4000
_TITLE_LIMIT = 500
_DEFAULT_LEASE = timedelta(minutes=5)
_INSERT_BATCH_SIZE = 1000


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class TelegramGateway(Protocol):
    async def send_message(self, telegram_id: int, text: str) -> int: ...


class TelegramSendError(Exception):
    def __init__(
        self,
        code: str,
        *,
        transient: bool = False,
        blocked: bool = False,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code[:128]
        self.transient = transient
        self.blocked = blocked
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class QuietHours:
    timezone: ZoneInfo
    start: time
    end: time

    def delivery_time(self, now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware datetime")
        local = now.astimezone(self.timezone)
        local_time = local.timetz().replace(tzinfo=None)
        if self.start == self.end:
            return now.astimezone(UTC)
        if self.start < self.end:
            quiet = self.start <= local_time < self.end
            release_date = local.date()
        else:
            quiet = local_time >= self.start or local_time < self.end
            release_date = local.date() + (
                timedelta(days=1) if local_time >= self.start else timedelta()
            )
        if not quiet:
            return now.astimezone(UTC)
        release = datetime.combine(release_date, self.end, tzinfo=self.timezone)
        return release.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DigestEntry:
    event_id: int
    delivery_ids: tuple[int, ...]
    notification_type: NotificationType
    title: str
    starts_at: datetime
    changed_fields: tuple[str, ...]
    url: str | None = None
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class DigestChunk:
    text: str
    delivery_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MaterializeResult:
    changes: int
    deliveries: int
    outboxes: int


@dataclass(frozen=True, slots=True)
class DeliveryBatchResult:
    claimed: int
    sent: int
    retrying: int
    failed: int
    blocked: int


class DigestRenderer:
    def __init__(
        self,
        localizations: LocalizationCatalog,
        *,
        timezone: ZoneInfo,
        text_limit: int = _SAFE_TEXT_LIMIT,
    ) -> None:
        if text_limit <= 0 or text_limit > _TELEGRAM_TEXT_LIMIT:
            raise ValueError("Telegram text limit is invalid")
        self._localizations = localizations
        self._timezone = timezone
        self._text_limit = text_limit

    def render(self, entries: Sequence[DigestEntry], locale: Locale) -> tuple[DigestChunk, ...]:
        sections = (
            (
                f"🆕 {self._text(locale, 'notifications.new_events')}",
                [item for item in entries if item.notification_type is NotificationType.NEW_EVENT],
            ),
            (
                f"✏️ {self._text(locale, 'notifications.important_changes')}",
                [
                    item
                    for item in entries
                    if item.notification_type is not NotificationType.NEW_EVENT
                ],
            ),
        )
        chunks: list[DigestChunk] = []
        for header, items in sections:
            if not items:
                continue
            current_lines = [header]
            current_ids: list[int] = []
            for item in items:
                entry = self._entry_text(item, locale)
                candidate = "\n\n".join((*current_lines, entry))
                if _telegram_length(candidate) > self._text_limit and len(current_lines) > 1:
                    chunks.append(DigestChunk("\n\n".join(current_lines), tuple(current_ids)))
                    current_lines = [header]
                    current_ids = []
                    candidate = "\n\n".join((header, entry))
                if _telegram_length(candidate) > self._text_limit:
                    allowed = max(self._text_limit - _telegram_length(header) - 2, 1)
                    entry = _truncate_telegram(entry, allowed)
                current_lines.append(entry)
                current_ids.extend(item.delivery_ids)
            chunks.append(DigestChunk("\n\n".join(current_lines), tuple(current_ids)))
        return tuple(chunks)

    def _entry_text(self, entry: DigestEntry, locale: Locale) -> str:
        title = " ".join(entry.title.split())[:_TITLE_LIMIT]
        kind = event_kind(entry.event_type)
        if kind is not None:
            title = f"{KIND_ICONS[kind]} {self._text(locale, f'kinds.{kind.value}')}: {title}"
        date = entry.starts_at.astimezone(self._timezone).strftime("%d.%m.%Y %H:%M")
        lines = [f"• {title}", f"📅 {date}"]
        if entry.notification_type is NotificationType.CANCELLATION:
            lines.append(f"❌ {self._text(locale, 'notifications.cancelled')}")
            return "\n".join(lines)
        if entry.url is not None:
            lines.append(f"🔗 {entry.url}")
        if entry.changed_fields:
            labels = [
                self._text(locale, f"notifications.fields.{field}")
                for field in entry.changed_fields
                if field
                in {
                    "address",
                    "format",
                    "is_available",
                    "registration_url",
                    "starts_at",
                    "state",
                    "titles",
                }
            ]
            if labels:
                lines.append(f"{self._text(locale, 'notifications.changed')}: {', '.join(labels)}")
        return "\n".join(lines)

    def _text(self, locale: Locale, key: str) -> str:
        return self._localizations.get(locale, key)


def _telegram_length(value: str) -> int:
    """Telegram limits message text in UTF-16 code units, where most emoji take two."""
    return len(value.encode("utf-16-le")) // 2


def _truncate_telegram(value: str, limit: int) -> str:
    # A surrogate pair cut in half is dropped rather than sent broken.
    return value.encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore")


class OutboxService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        renderer: DigestRenderer,
        quiet_hours: QuietHours,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._renderer = renderer
        self._quiet_hours = quiet_hours
        self._clock = clock or SystemClock()

    async def materialize(self, *, limit: int = 200) -> MaterializeResult:
        if limit <= 0:
            raise ValueError("Materialization limit must be positive")
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                changes = list(
                    (
                        await session.scalars(
                            select(DomainChange)
                            .where(
                                DomainChange.materialized_at.is_(None),
                                ~select(EventClassification.event_id)
                                .where(
                                    EventClassification.event_id == DomainChange.event_id,
                                    EventClassification.status.in_(
                                        (
                                            ClassificationStatus.PENDING,
                                            ClassificationStatus.RETRY_WAIT,
                                        )
                                    ),
                                )
                                .exists(),
                            )
                            .order_by(DomainChange.id)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                if not changes:
                    return MaterializeResult(0, 0, 0)
                ended_event_ids, started_event_ids = await self._past_event_ids(
                    session, {change.event_id for change in changes}, now
                )
                inserted = 0
                for change in changes:
                    # Source updates once the event has started (recording links, archive
                    # cleanup) are not actionable for subscribers. A new event stays
                    # current until the end of its local day, like in the catalog.
                    silenced = change.event_id in (
                        started_event_ids
                        if change.notification_type is not NotificationType.NEW_EVENT
                        else ended_event_ids
                    )
                    recipient_ids = () if silenced else await self._recipient_ids(session, change)
                    if recipient_ids:
                        for offset in range(0, len(recipient_ids), _INSERT_BATCH_SIZE):
                            batch = recipient_ids[offset : offset + _INSERT_BATCH_SIZE]
                            statement = pg_insert(NotificationDelivery).values(
                                [
                                    {
                                        "user_id": user_id,
                                        "domain_change_id": change.id,
                                        "event_id": change.event_id,
                                        "event_revision": change.event_revision,
                                        "notification_type": change.notification_type,
                                        "status": DeliveryStatus.PENDING,
                                    }
                                    for user_id in batch
                                ]
                            )
                            result = await session.execute(
                                statement.on_conflict_do_nothing(
                                    constraint="uq_notification_deliveries_idempotency"
                                ).returning(NotificationDelivery.id)
                            )
                            inserted += len(result.scalars().all())
                    change.materialized_at = now
                change_ids = [change.id for change in changes]
                user_ids = tuple(
                    (
                        await session.scalars(
                            select(NotificationDelivery.user_id)
                            .where(
                                NotificationDelivery.domain_change_id.in_(change_ids),
                                NotificationDelivery.status == DeliveryStatus.PENDING,
                                NotificationDelivery.outbox_id.is_(None),
                            )
                            .distinct()
                            .order_by(NotificationDelivery.user_id)
                        )
                    ).all()
                )
                outboxes = 0
                for user_id in user_ids:
                    if await self._bundle_user(session, user_id, now):
                        outboxes += 1
                return MaterializeResult(len(changes), inserted, outboxes)

    async def _past_event_ids(
        self, session: AsyncSession, event_ids: set[int], now: datetime
    ) -> tuple[set[int], set[int]]:
        """Return events whose local day has ended and events that have started."""
        timezone = self._quiet_hours.timezone
        today_start = datetime.combine(now.astimezone(timezone).date(), time.min, timezone)
        rows = (
            await session.execute(
                select(Event.id, Event.starts_at).where(
                    Event.id.in_(event_ids), Event.starts_at <= now
                )
            )
        ).tuples()
        started: set[int] = set()
        ended: set[int] = set()
        for event_id, starts_at in rows:
            started.add(event_id)
            if starts_at < today_start:
                ended.add(event_id)
        return ended, started

    async def _recipient_ids(self, session: AsyncSession, change: DomainChange) -> tuple[int, ...]:
        event_type = await session.scalar(
            select(Event.event_type).where(Event.id == change.event_id)
        )
        if is_program(event_type):
            # Programs have their own opt-in, independent of event categories.
            return tuple(
                (
                    await session.scalars(
                        select(User.id)
                        .where(
                            User.is_active.is_(True),
                            User.access_granted.is_(True),
                            User.program_notifications.is_(True),
                        )
                        .order_by(User.id)
                    )
                ).all()
            )
        category_values = set(change.old_categories) | set(change.new_categories)
        categories = [
            category
            for value in category_values
            if (category := CategorySlug._value2member_map_.get(value)) is not None
        ]
        subscription_condition: ColumnElement[bool] = SubscriptionSettings.subscribe_all.is_(True)
        if categories:
            subscription_condition = or_(
                subscription_condition,
                select(SubscriptionCategory.user_id)
                .where(
                    SubscriptionCategory.user_id == User.id,
                    SubscriptionCategory.category.in_(categories),
                )
                .exists(),
            )
        return tuple(
            (
                await session.scalars(
                    select(User.id)
                    .join(SubscriptionSettings, SubscriptionSettings.user_id == User.id)
                    .where(
                        User.is_active.is_(True),
                        User.access_granted.is_(True),
                        subscription_condition,
                    )
                    .order_by(User.id)
                )
            ).all()
        )

    async def _bundle_user(self, session: AsyncSession, user_id: int, now: datetime) -> bool:
        user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            return False
        pending_ids = tuple(
            (
                await session.scalars(
                    select(NotificationDelivery.id)
                    .where(
                        NotificationDelivery.user_id == user_id,
                        NotificationDelivery.status == DeliveryStatus.PENDING,
                        NotificationDelivery.outbox_id.is_(None),
                    )
                    .order_by(NotificationDelivery.id)
                )
            ).all()
        )
        if not pending_ids:
            return False
        if not user.is_active or not user.access_granted:
            await session.execute(
                update(NotificationDelivery)
                .where(NotificationDelivery.id.in_(pending_ids))
                .values(status=DeliveryStatus.BLOCKED, last_error_code="inactive_user")
            )
            return False
        available_at = self._quiet_hours.delivery_time(now)
        quiet = available_at > now
        if quiet:
            batch_key = f"quiet:{user_id}:{available_at.isoformat()}"
            outbox = await session.scalar(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.batch_key == batch_key,
                    NotificationOutbox.status == DeliveryStatus.PENDING,
                )
                .with_for_update()
            )
        else:
            digest = hashlib.sha256(
                ",".join(str(item) for item in pending_ids).encode()
            ).hexdigest()
            batch_key = f"digest:{user_id}:{digest}"
            outbox = None
        created = outbox is None
        if outbox is None:
            outbox = NotificationOutbox(
                user_id=user_id,
                batch_id=uuid4(),
                batch_key=batch_key,
                status=DeliveryStatus.PENDING,
                payload={"version": 1, "chunks": []},
                available_at=available_at,
            )
            session.add(outbox)
            await session.flush()
        await session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id.in_(pending_ids))
            .values(outbox_id=outbox.id)
        )
        entries = await self._digest_entries(session, outbox.id, user.locale)
        chunks = self._renderer.render(entries, user.locale)
        if not chunks:
            raise RuntimeError("Outbox digest cannot be empty")
        outbox.payload = {
            "version": 1,
            "locale": user.locale.value,
            "chunks": [
                {"text": chunk.text, "delivery_ids": list(chunk.delivery_ids)} for chunk in chunks
            ],
        }
        return created

    @staticmethod
    async def _digest_entries(
        session: AsyncSession, outbox_id: int, locale: Locale
    ) -> tuple[DigestEntry, ...]:
        rows = (
            await session.execute(
                select(
                    NotificationDelivery.id,
                    NotificationDelivery.event_id,
                    NotificationDelivery.event_revision,
                    NotificationDelivery.notification_type,
                    DomainChange.change_data,
                    Event.starts_at,
                    Event.registration_url,
                    Event.recording_url,
                    Event.event_type,
                )
                .join(DomainChange, DomainChange.id == NotificationDelivery.domain_change_id)
                .join(Event, Event.id == NotificationDelivery.event_id)
                .where(NotificationDelivery.outbox_id == outbox_id)
                .order_by(NotificationDelivery.event_id, NotificationDelivery.event_revision)
            )
        ).all()
        event_ids = sorted({row.event_id for row in rows})
        localizations = (
            (
                await session.scalars(
                    select(EventLocalization).where(EventLocalization.event_id.in_(event_ids))
                )
            ).all()
            if event_ids
            else []
        )
        titles: dict[int, dict[Locale, str]] = defaultdict(dict)
        details_urls: dict[int, dict[Locale, str | None]] = defaultdict(dict)
        for localization in localizations:
            titles[localization.event_id][localization.locale] = localization.title
            details_urls[localization.event_id][localization.locale] = localization.details_url
        grouped: dict[int, list[Any]] = defaultdict(list)
        for row in rows:
            grouped[row.event_id].append(row)
        entries: list[DigestEntry] = []
        priority = {
            NotificationType.IMPORTANT_CHANGE: 1,
            NotificationType.NEW_EVENT: 2,
            NotificationType.CANCELLATION: 3,
        }
        for event_id, event_rows in grouped.items():
            selected = max(
                event_rows,
                key=lambda row: (priority[row.notification_type], row.event_revision),
            )
            localized = titles[event_id]
            title = localized.get(locale) or localized.get(Locale.RU) or localized.get(Locale.KZ)
            details = details_urls[event_id]
            details_url = details.get(locale) or details.get(Locale.RU) or details.get(Locale.KZ)
            entries.append(
                DigestEntry(
                    event_id=event_id,
                    delivery_ids=tuple(row.id for row in event_rows),
                    notification_type=selected.notification_type,
                    title=title or f"Event {event_id}",
                    starts_at=selected.starts_at,
                    changed_fields=tuple(sorted(selected.change_data)),
                    url=(
                        registration_link(selected.registration_url, details_url)
                        or normalize_http_url(selected.recording_url or "")
                    ),
                    event_type=selected.event_type,
                )
            )
        return tuple(
            sorted(
                entries,
                key=lambda item: (
                    item.notification_type is not NotificationType.NEW_EVENT,
                    item.starts_at,
                    item.event_id,
                ),
            )
        )

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Outbox clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


class DeliveryWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: TelegramGateway,
        *,
        clock: Clock | None = None,
        lease: timedelta = _DEFAULT_LEASE,
    ) -> None:
        if lease <= timedelta():
            raise ValueError("Delivery lease must be positive")
        self._session_factory = session_factory
        self._gateway = gateway
        self._clock = clock or SystemClock()
        self._lease = lease

    async def run_batch(self, *, limit: int = 50) -> DeliveryBatchResult:
        if limit <= 0:
            raise ValueError("Delivery batch limit must be positive")
        outbox_ids = await self._claim(limit)
        counts: defaultdict[str, int] = defaultdict(int)
        for outbox_id in outbox_ids:
            outcome = await self._deliver(outbox_id)
            counts[outcome] += 1
        return DeliveryBatchResult(
            claimed=len(outbox_ids),
            sent=counts["sent"],
            retrying=counts["retrying"],
            failed=counts["failed"],
            blocked=counts["blocked"],
        )

    async def _claim(self, limit: int) -> tuple[int, ...]:
        now = self._now()
        stale_before = now - self._lease
        async with self._session_factory() as session:
            async with session.begin():
                rows = list(
                    (
                        await session.scalars(
                            select(NotificationOutbox)
                            .where(
                                or_(
                                    and_(
                                        NotificationOutbox.status.in_(
                                            (
                                                DeliveryStatus.PENDING,
                                                DeliveryStatus.RETRY_WAIT,
                                            )
                                        ),
                                        NotificationOutbox.available_at <= now,
                                    ),
                                    and_(
                                        NotificationOutbox.status == DeliveryStatus.PROCESSING,
                                        or_(
                                            NotificationOutbox.locked_at.is_(None),
                                            NotificationOutbox.locked_at <= stale_before,
                                        ),
                                    ),
                                )
                            )
                            .order_by(NotificationOutbox.available_at, NotificationOutbox.id)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).all()
                )
                for outbox in rows:
                    outbox.status = DeliveryStatus.PROCESSING
                    outbox.locked_at = now
                    outbox.attempts += 1
                    await session.execute(
                        update(NotificationDelivery)
                        .where(
                            NotificationDelivery.outbox_id == outbox.id,
                            NotificationDelivery.status != DeliveryStatus.SENT,
                        )
                        .values(
                            status=DeliveryStatus.PROCESSING,
                            attempts=NotificationDelivery.attempts + 1,
                        )
                    )
                return tuple(row.id for row in rows)

    async def _deliver(self, outbox_id: int) -> str:
        async with self._session_factory() as session:
            outbox = await session.get(NotificationOutbox, outbox_id)
            if outbox is None or outbox.status is not DeliveryStatus.PROCESSING:
                return "failed"
            user = await session.get(User, outbox.user_id)
            if user is None or not user.is_active or not user.access_granted:
                await self._block_user(outbox.user_id, "inactive_user")
                return "blocked"
            telegram_id = user.telegram_id
            sent_count = len(outbox.telegram_message_ids)
            try:
                chunks = self._payload_chunks(outbox.payload)
            except ValueError:
                await self._fail(outbox_id, "invalid_payload")
                return "failed"
        for chunk in chunks[sent_count:]:
            try:
                message_id = await self._gateway.send_message(telegram_id, chunk.text)
            except TelegramSendError as error:
                if error.blocked:
                    await self._block_user(outbox.user_id, error.code)
                    return "blocked"
                if error.transient:
                    await self._retry(outbox_id, error)
                    return "retrying"
                await self._fail(outbox_id, error.code)
                return "failed"
            await self._ack_chunk(outbox_id, message_id, chunk.delivery_ids)
        return "sent" if await self._complete(outbox_id) else "failed"

    @staticmethod
    def _payload_chunks(payload: Mapping[str, Any]) -> tuple[DigestChunk, ...]:
        if payload.get("version") != 1 or not isinstance(payload.get("chunks"), list):
            raise ValueError("Unsupported outbox payload")
        chunks: list[DigestChunk] = []
        for raw in payload["chunks"]:
            if not isinstance(raw, dict):
                raise ValueError("Invalid outbox chunk")
            text = raw.get("text")
            delivery_ids = raw.get("delivery_ids")
            if (
                not isinstance(text, str)
                or not text
                or len(text) > _TELEGRAM_TEXT_LIMIT
                or not isinstance(delivery_ids, list)
                or not delivery_ids
                or any(not isinstance(item, int) or isinstance(item, bool) for item in delivery_ids)
            ):
                raise ValueError("Invalid outbox chunk")
            chunks.append(DigestChunk(text=text, delivery_ids=tuple(delivery_ids)))
        if not chunks:
            raise ValueError("Empty outbox payload")
        return tuple(chunks)

    async def _ack_chunk(
        self, outbox_id: int, message_id: int, delivery_ids: tuple[int, ...]
    ) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(NotificationOutbox, outbox_id, with_for_update=True)
                if outbox is None or outbox.status is not DeliveryStatus.PROCESSING:
                    raise RuntimeError("Claimed outbox is no longer processing")
                outbox.telegram_message_ids = [*outbox.telegram_message_ids, message_id]
                outbox.locked_at = now
                await session.execute(
                    update(NotificationDelivery)
                    .where(
                        NotificationDelivery.outbox_id == outbox_id,
                        NotificationDelivery.id.in_(delivery_ids),
                        NotificationDelivery.status == DeliveryStatus.PROCESSING,
                    )
                    .values(status=DeliveryStatus.SENT, sent_at=now, next_attempt_at=None)
                )

    async def _complete(self, outbox_id: int) -> bool:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(NotificationOutbox, outbox_id, with_for_update=True)
                if outbox is None:
                    return False
                remaining = await session.scalar(
                    select(NotificationDelivery.id).where(
                        NotificationDelivery.outbox_id == outbox_id,
                        NotificationDelivery.status != DeliveryStatus.SENT,
                    )
                )
                if remaining is not None:
                    outbox.status = DeliveryStatus.FAILED
                    outbox.locked_at = None
                    outbox.last_error_code = "incomplete_payload"
                    await session.execute(
                        update(NotificationDelivery)
                        .where(
                            NotificationDelivery.outbox_id == outbox_id,
                            NotificationDelivery.status != DeliveryStatus.SENT,
                        )
                        .values(
                            status=DeliveryStatus.FAILED,
                            last_error_code="incomplete_payload",
                        )
                    )
                    return False
                outbox.status = DeliveryStatus.SENT
                outbox.sent_at = now
                outbox.locked_at = None
                outbox.last_error_code = None
                return True

    async def _retry(self, outbox_id: int, error: TelegramSendError) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(NotificationOutbox, outbox_id, with_for_update=True)
                if outbox is None:
                    return
                backoff = timedelta(seconds=min(30 * (2 ** min(outbox.attempts - 1, 7)), 3600))
                delay = max(backoff, error.retry_after or timedelta())
                next_attempt = now + delay
                outbox.status = DeliveryStatus.RETRY_WAIT
                outbox.available_at = next_attempt
                outbox.locked_at = None
                outbox.last_error_code = error.code
                await session.execute(
                    update(NotificationDelivery)
                    .where(
                        NotificationDelivery.outbox_id == outbox_id,
                        NotificationDelivery.status == DeliveryStatus.PROCESSING,
                    )
                    .values(
                        status=DeliveryStatus.RETRY_WAIT,
                        next_attempt_at=next_attempt,
                        last_error_code=error.code,
                    )
                )

    async def _fail(self, outbox_id: int, code: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                outbox = await session.get(NotificationOutbox, outbox_id, with_for_update=True)
                if outbox is None:
                    return
                outbox.status = DeliveryStatus.FAILED
                outbox.locked_at = None
                outbox.last_error_code = code[:128]
                await session.execute(
                    update(NotificationDelivery)
                    .where(
                        NotificationDelivery.outbox_id == outbox_id,
                        NotificationDelivery.status != DeliveryStatus.SENT,
                    )
                    .values(status=DeliveryStatus.FAILED, last_error_code=code[:128])
                )

    async def _block_user(self, user_id: int, code: str) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
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
                    .values(
                        status=DeliveryStatus.BLOCKED,
                        locked_at=None,
                        last_error_code=code[:128],
                    )
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
                for broadcast_id in sorted(broadcast_ids):
                    broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
                    if broadcast is None:
                        continue
                    count_rows = (
                        await session.execute(
                            select(BroadcastDelivery.status, func.count())
                            .where(BroadcastDelivery.broadcast_id == broadcast_id)
                            .group_by(BroadcastDelivery.status)
                        )
                    ).all()
                    counts = {status: count for status, count in count_rows}
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
                    if unfinished == 0 and broadcast.status in (
                        BroadcastStatus.READY,
                        BroadcastStatus.RUNNING,
                    ):
                        broadcast.status = BroadcastStatus.COMPLETED
                        broadcast.completed_at = now

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Delivery clock must return a timezone-aware datetime")
        return now.astimezone(UTC)
