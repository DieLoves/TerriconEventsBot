from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from terricon_events_bot.domain.enums import (
    AccessMode,
    BroadcastStatus,
    DeliveryStatus,
    Locale,
    TicketStatus,
)
from terricon_events_bot.infrastructure.models import (
    BetaAllowlistEntry,
    Broadcast,
    BroadcastDelivery,
    FeedbackTicket,
    FsmState,
    SubscriptionSettings,
    User,
)


@dataclass(frozen=True, slots=True)
class TelegramPrincipal:
    telegram_id: int
    username: str | None
    display_name: str | None

    def __post_init__(self) -> None:
        if self.telegram_id <= 0:
            raise ValueError("Telegram ID must be positive")


@dataclass(frozen=True, slots=True)
class UserContext:
    user: User
    is_admin: bool


class UserService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        access_mode: AccessMode,
        allowed_telegram_ids: frozenset[int],
        admin_telegram_ids: frozenset[int],
    ) -> None:
        self._session_factory = session_factory
        self._access_mode = access_mode
        self._allowed_telegram_ids = allowed_telegram_ids
        self._admin_telegram_ids = admin_telegram_ids

    async def resolve(self, principal: TelegramPrincipal) -> UserContext | None:
        is_admin = principal.telegram_id in self._admin_telegram_ids
        async with self._session_factory() as session:
            async with session.begin():
                allowed = await self._is_allowed(session, principal.telegram_id, is_admin)
                if not allowed:
                    await session.execute(
                        update(User)
                        .where(User.telegram_id == principal.telegram_id)
                        .values(access_granted=False)
                    )
                    return None
                now = datetime.now(UTC)
                statement = pg_insert(User).values(
                    telegram_id=principal.telegram_id,
                    username=self._normalize_username(principal.username),
                    display_name=self._normalize_display_name(principal.display_name),
                    is_active=True,
                    access_granted=True,
                    last_active_at=now,
                )
                row = (
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[User.telegram_id],
                            set_={
                                "username": statement.excluded.username,
                                "display_name": statement.excluded.display_name,
                                "is_active": True,
                                "access_granted": True,
                                "last_active_at": now,
                            },
                        ).returning(
                            User,
                            # created_at defaults to now(), the transaction start time.
                            (User.created_at == func.now()).label("created"),
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise RuntimeError("User upsert did not return a persisted user")
                user, created = row
                if created:
                    # New users start subscribed to all events; an explicit unsubscribe
                    # later is kept because existing users are never resubscribed here.
                    await session.execute(
                        pg_insert(SubscriptionSettings)
                        .values(user_id=user.id, subscribe_all=True)
                        .on_conflict_do_nothing()
                    )
                return UserContext(user=user, is_admin=is_admin)

    async def reconcile_access(self) -> int:
        """Apply the configured access mode to stored users; return changed rows.

        Without this, a user removed from ALLOWED_TELEGRAM_IDS would keep receiving
        notifications and broadcasts until their next interaction with the bot.
        """
        if self._access_mode is AccessMode.PUBLIC:
            permitted: ColumnElement[bool] = true()
        else:
            permitted = or_(
                User.telegram_id.in_(self._allowed_telegram_ids | self._admin_telegram_ids),
                select(BetaAllowlistEntry.telegram_id)
                .where(BetaAllowlistEntry.telegram_id == User.telegram_id)
                .exists(),
            )
        async with self._session_factory() as session:
            async with session.begin():
                changed = await session.scalars(
                    update(User)
                    .where(User.access_granted.is_distinct_from(permitted))
                    .values(access_granted=permitted)
                    .returning(User.id)
                )
                return len(changed.all())

    async def set_locale(self, user_id: int, locale: Locale) -> None:
        await self._update_user(user_id, locale=locale)

    async def complete_onboarding(self, user_id: int) -> None:
        await self._update_user(user_id, onboarding_completed=True)

    async def toggle_menu_images(self, user_id: int) -> bool:
        return await self._toggle(user_id, "menu_images_enabled")

    async def toggle_event_posters(self, user_id: int) -> bool:
        return await self._toggle(user_id, "event_posters_enabled")

    async def clear_catalog_state(self, user_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(FsmState).where(
                        FsmState.user_id == user_id,
                        FsmState.scope == "catalog",
                    )
                )

    async def deactivate(self, user_id: int) -> None:
        await self._update_user(user_id, is_active=False)

    async def delete_profile(self, user_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.id == user_id).with_for_update()
                )
                if user is None:
                    return
                now = datetime.now(UTC)
                affected_broadcast_ids = tuple(
                    (
                        await session.scalars(
                            select(BroadcastDelivery.broadcast_id)
                            .where(BroadcastDelivery.user_id == user_id)
                            .distinct()
                        )
                    ).all()
                )
                await session.execute(
                    update(FeedbackTicket)
                    .where(
                        FeedbackTicket.user_id == user_id,
                        FeedbackTicket.status != TicketStatus.CLOSED,
                    )
                    .values(
                        status=TicketStatus.CLOSED,
                        closed_at=now,
                        updated_at=now,
                    )
                )
                await session.delete(user)
                await session.flush()
                for broadcast_id in affected_broadcast_ids:
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
                    counts: dict[DeliveryStatus, int] = {
                        status: count for status, count in count_rows
                    }
                    broadcast.total_count = sum(counts.values())
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

    async def _is_allowed(self, session: AsyncSession, telegram_id: int, is_admin: bool) -> bool:
        if self._access_mode is AccessMode.PUBLIC or is_admin:
            return True
        if telegram_id in self._allowed_telegram_ids:
            return True
        return (await session.get(BetaAllowlistEntry, telegram_id)) is not None

    async def _update_user(self, user_id: int, **values: object) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                updated_id = await session.scalar(
                    update(User).where(User.id == user_id).values(**values).returning(User.id)
                )
                if updated_id is None:
                    raise LookupError("User does not exist")

    async def _toggle(
        self,
        user_id: int,
        attribute: Literal["menu_images_enabled", "event_posters_enabled"],
    ) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                user = await session.get(User, user_id, with_for_update=True)
                if user is None:
                    raise LookupError("User does not exist")
                value = not getattr(user, attribute)
                setattr(user, attribute, value)
                return value

    @staticmethod
    def _normalize_username(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lstrip("@")
        return normalized[:64] or None

    @staticmethod
    def _normalize_display_name(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized[:255] or None
