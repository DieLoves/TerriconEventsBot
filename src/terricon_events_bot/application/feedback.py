from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.domain.enums import (
    FeedbackAuthor,
    FeedbackKind,
    TicketStatus,
)
from terricon_events_bot.infrastructure.models import (
    FeedbackMessage,
    FeedbackTicket,
    FsmState,
    User,
)

_FEEDBACK_SCOPE = "feedback"
_ADMIN_REPLY_SCOPE = "admin_reply"
_TEXT_LIMIT = 4000
_FILE_ID_LIMIT = 255


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FeedbackError(ValueError):
    pass


class FeedbackBlockedError(FeedbackError):
    pass


class FeedbackRateLimitedError(FeedbackError):
    pass


class FeedbackStateError(FeedbackError):
    pass


@dataclass(frozen=True, slots=True)
class FeedbackDraft:
    kind: FeedbackKind
    body: str | None
    telegram_file_id: str | None
    state: str


@dataclass(frozen=True, slots=True)
class FeedbackNotification:
    ticket_id: int
    user_id: int
    telegram_id: int
    kind: FeedbackKind
    display_name: str | None
    username: str | None
    body: str
    telegram_file_id: str | None
    continuation: bool = False


@dataclass(frozen=True, slots=True)
class FeedbackTicketView:
    ticket_id: int
    user_id: int
    telegram_id: int
    kind: FeedbackKind
    status: TicketStatus
    display_name: str | None
    username: str | None
    messages: tuple[tuple[FeedbackAuthor, str | None, str | None], ...]


class FeedbackService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def begin(self, user_id: int, kind: FeedbackKind) -> FeedbackDraft:
        async with self._session_factory() as session:
            async with session.begin():
                user = await self._lock_user(session, user_id)
                self._ensure_not_blocked(user)
                await session.execute(
                    pg_insert(FsmState)
                    .values(
                        user_id=user_id,
                        scope=_FEEDBACK_SCOPE,
                        state="text",
                        data={"kind": kind.value},
                    )
                    .on_conflict_do_update(
                        index_elements=[FsmState.user_id, FsmState.scope],
                        set_={
                            "state": "text",
                            "data": {"kind": kind.value},
                            "expires_at": None,
                            "updated_at": self._now(),
                        },
                    )
                )
        return FeedbackDraft(kind=kind, body=None, telegram_file_id=None, state="text")

    async def set_text(self, user_id: int, body: str) -> FeedbackDraft:
        normalized = self._normalize_text(body)
        async with self._session_factory() as session:
            async with session.begin():
                state = await self._state(session, user_id, for_update=True)
                if state is None or state.state != "text":
                    raise FeedbackStateError("Feedback text is not expected")
                kind = self._kind(state.data)
                state.state = "photo"
                state.data = {**state.data, "body": normalized}
        return FeedbackDraft(kind, normalized, None, "photo")

    async def set_photo(self, user_id: int, telegram_file_id: str | None) -> FeedbackDraft:
        file_id = self._normalize_file_id(telegram_file_id)
        async with self._session_factory() as session:
            async with session.begin():
                state = await self._state(session, user_id, for_update=True)
                if state is None or state.state != "photo":
                    raise FeedbackStateError("Feedback photo is not expected")
                draft = self._draft(state)
                if draft.body is None:
                    raise FeedbackStateError("Feedback body is missing")
                state.state = "preview"
                state.data = {**state.data, "telegram_file_id": file_id}
        return FeedbackDraft(draft.kind, draft.body, file_id, "preview")

    async def draft(self, user_id: int) -> FeedbackDraft | None:
        async with self._session_factory() as session:
            state = await self._state(session, user_id)
            return self._draft(state) if state is not None else None

    async def cancel(self, user_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(FsmState).where(
                        FsmState.user_id == user_id,
                        FsmState.scope == _FEEDBACK_SCOPE,
                    )
                )

    async def confirm(self, user_id: int) -> FeedbackNotification:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                user = await self._lock_user(session, user_id)
                self._ensure_not_blocked(user)
                state = await self._state(session, user_id, for_update=True)
                if state is None or state.state != "preview":
                    raise FeedbackStateError("Feedback preview is not ready")
                draft = self._draft(state)
                if draft.body is None:
                    raise FeedbackStateError("Feedback body is missing")
                recent = await session.scalar(
                    select(func.count())
                    .select_from(FeedbackTicket)
                    .where(
                        FeedbackTicket.user_id == user_id,
                        FeedbackTicket.created_at >= now - timedelta(hours=24),
                    )
                )
                if (recent or 0) >= 3:
                    raise FeedbackRateLimitedError("Feedback rate limit exceeded")
                ticket = FeedbackTicket(
                    user_id=user_id,
                    kind=draft.kind,
                    status=TicketStatus.AWAITING_ADMIN,
                    created_at=now,
                    updated_at=now,
                )
                session.add(ticket)
                await session.flush()
                session.add(
                    FeedbackMessage(
                        ticket_id=ticket.id,
                        author=FeedbackAuthor.USER,
                        body=draft.body,
                        telegram_file_id=draft.telegram_file_id,
                        created_at=now,
                    )
                )
                await session.delete(state)
                return self._notification(ticket, user, draft.body, draft.telegram_file_id)

    async def add_user_reply(
        self,
        user_id: int,
        *,
        body: str | None,
        telegram_file_id: str | None = None,
    ) -> FeedbackNotification:
        normalized_body = self._normalize_optional_text(body)
        file_id = self._normalize_file_id(telegram_file_id)
        if normalized_body is None and file_id is None:
            raise FeedbackError("Feedback reply must have content")
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                user = await self._lock_user(session, user_id)
                self._ensure_not_blocked(user)
                ticket = await session.scalar(
                    select(FeedbackTicket)
                    .where(
                        FeedbackTicket.user_id == user_id,
                        FeedbackTicket.status == TicketStatus.AWAITING_USER,
                    )
                    .order_by(FeedbackTicket.updated_at.desc(), FeedbackTicket.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                if ticket is None:
                    raise FeedbackStateError("No feedback ticket is awaiting a user reply")
                ticket.status = TicketStatus.AWAITING_ADMIN
                ticket.updated_at = now
                session.add(
                    FeedbackMessage(
                        ticket_id=ticket.id,
                        author=FeedbackAuthor.USER,
                        body=normalized_body,
                        telegram_file_id=file_id,
                        created_at=now,
                    )
                )
                return self._notification(
                    ticket,
                    user,
                    normalized_body or "",
                    file_id,
                    continuation=True,
                )

    async def attach_admin_message(
        self, ticket_id: int, admin_chat_id: int, admin_message_id: int
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                ticket = await session.get(FeedbackTicket, ticket_id, with_for_update=True)
                if ticket is None:
                    raise LookupError("Feedback ticket does not exist")
                ticket.admin_chat_id = admin_chat_id
                ticket.admin_message_id = admin_message_id

    async def list_open(self, *, limit: int = 20) -> tuple[FeedbackTicketView, ...]:
        if limit <= 0:
            raise ValueError("Ticket limit must be positive")
        async with self._session_factory() as session:
            ticket_ids = tuple(
                (
                    await session.scalars(
                        select(FeedbackTicket.id)
                        .where(FeedbackTicket.status != TicketStatus.CLOSED)
                        .order_by(FeedbackTicket.updated_at.desc(), FeedbackTicket.id.desc())
                        .limit(limit)
                    )
                ).all()
            )
            views = [await self._ticket_view(session, ticket_id) for ticket_id in ticket_ids]
        return tuple(view for view in views if view is not None)

    async def ticket(self, ticket_id: int) -> FeedbackTicketView | None:
        async with self._session_factory() as session:
            return await self._ticket_view(session, ticket_id)

    async def begin_admin_reply(self, admin_user_id: int, ticket_id: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._lock_user(session, admin_user_id)
                ticket = await session.get(FeedbackTicket, ticket_id)
                if ticket is None or ticket.status is TicketStatus.CLOSED:
                    raise LookupError("Open feedback ticket does not exist")
                await session.execute(
                    pg_insert(FsmState)
                    .values(
                        user_id=admin_user_id,
                        scope=_ADMIN_REPLY_SCOPE,
                        state="text",
                        data={"ticket_id": ticket_id},
                    )
                    .on_conflict_do_update(
                        index_elements=[FsmState.user_id, FsmState.scope],
                        set_={
                            "state": "text",
                            "data": {"ticket_id": ticket_id},
                            "updated_at": self._now(),
                        },
                    )
                )

    async def pending_admin_reply(self, admin_user_id: int) -> int | None:
        async with self._session_factory() as session:
            state = await session.get(FsmState, (admin_user_id, _ADMIN_REPLY_SCOPE))
            if state is None or state.state != "text":
                return None
            ticket_id = state.data.get("ticket_id")
            return (
                ticket_id
                if isinstance(ticket_id, int) and not isinstance(ticket_id, bool)
                else None
            )

    async def complete_admin_reply(
        self,
        admin_user_id: int,
        ticket_id: int,
        body: str,
        telegram_message_id: int,
    ) -> None:
        normalized = self._normalize_text(body)
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                state = await session.get(
                    FsmState, (admin_user_id, _ADMIN_REPLY_SCOPE), with_for_update=True
                )
                if state is None or state.data.get("ticket_id") != ticket_id:
                    raise FeedbackStateError("Admin reply state is stale")
                ticket = await session.get(FeedbackTicket, ticket_id, with_for_update=True)
                if ticket is None or ticket.status is TicketStatus.CLOSED:
                    raise LookupError("Open feedback ticket does not exist")
                session.add(
                    FeedbackMessage(
                        ticket_id=ticket_id,
                        author=FeedbackAuthor.ADMIN,
                        body=normalized,
                        telegram_message_id=telegram_message_id,
                        created_at=now,
                    )
                )
                ticket.status = TicketStatus.AWAITING_USER
                ticket.updated_at = now
                await session.delete(state)

    async def close(self, ticket_id: int) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                ticket = await session.get(FeedbackTicket, ticket_id, with_for_update=True)
                if ticket is None:
                    raise LookupError("Feedback ticket does not exist")
                ticket.status = TicketStatus.CLOSED
                ticket.closed_at = now
                ticket.updated_at = now

    async def set_blocked(
        self,
        ticket_id: int,
        *,
        blocked: bool,
        admin_telegram_id: int,
    ) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                ticket = await session.get(FeedbackTicket, ticket_id)
                if ticket is None:
                    raise LookupError("Feedback ticket does not exist")
                user = await self._lock_user(session, ticket.user_id)
                user.feedback_blocked_at = now if blocked else None
                user.feedback_blocked_by = admin_telegram_id if blocked else None

    async def cleanup(self, retention_days: int) -> int:
        if retention_days <= 0:
            raise ValueError("Feedback retention must be positive")
        cutoff = self._now() - timedelta(days=retention_days)
        async with self._session_factory() as session:
            async with session.begin():
                deleted = await session.scalars(
                    delete(FeedbackTicket)
                    .where(
                        FeedbackTicket.status == TicketStatus.CLOSED,
                        FeedbackTicket.closed_at < cutoff,
                    )
                    .returning(FeedbackTicket.id)
                )
                return len(deleted.all())

    async def _ticket_view(
        self, session: AsyncSession, ticket_id: int
    ) -> FeedbackTicketView | None:
        row = (
            await session.execute(
                select(FeedbackTicket, User)
                .join(User, User.id == FeedbackTicket.user_id)
                .where(FeedbackTicket.id == ticket_id)
            )
        ).one_or_none()
        if row is None:
            return None
        ticket, user = row
        messages = (
            await session.scalars(
                select(FeedbackMessage)
                .where(FeedbackMessage.ticket_id == ticket_id)
                .order_by(FeedbackMessage.created_at, FeedbackMessage.id)
            )
        ).all()
        return FeedbackTicketView(
            ticket_id=ticket.id,
            user_id=user.id,
            telegram_id=user.telegram_id,
            kind=ticket.kind,
            status=ticket.status,
            display_name=user.display_name,
            username=user.username,
            messages=tuple(
                (message.author, message.body, message.telegram_file_id) for message in messages
            ),
        )

    async def _lock_user(self, session: AsyncSession, user_id: int) -> User:
        user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None:
            raise LookupError("User does not exist")
        return user

    @staticmethod
    async def _state(
        session: AsyncSession, user_id: int, *, for_update: bool = False
    ) -> FsmState | None:
        statement = select(FsmState).where(
            FsmState.user_id == user_id,
            FsmState.scope == _FEEDBACK_SCOPE,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(FsmState | None, await session.scalar(statement))

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Feedback clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _ensure_not_blocked(user: User) -> None:
        if user.feedback_blocked_at is not None:
            raise FeedbackBlockedError("Feedback is blocked")

    @staticmethod
    def _normalize_text(body: str) -> str:
        normalized = body.strip()
        if not normalized:
            raise FeedbackError("Feedback text must not be empty")
        if len(normalized) > _TEXT_LIMIT:
            raise FeedbackError("Feedback text is too long")
        return normalized

    @classmethod
    def _normalize_optional_text(cls, body: str | None) -> str | None:
        return cls._normalize_text(body) if body is not None else None

    @staticmethod
    def _normalize_file_id(telegram_file_id: str | None) -> str | None:
        if telegram_file_id is None:
            return None
        normalized = telegram_file_id.strip()
        if not normalized or len(normalized) > _FILE_ID_LIMIT:
            raise FeedbackError("Telegram file ID is invalid")
        return normalized

    @staticmethod
    def _kind(data: dict[str, object]) -> FeedbackKind:
        value = data.get("kind")
        if not isinstance(value, str):
            raise FeedbackStateError("Feedback kind is invalid")
        try:
            return FeedbackKind(value)
        except ValueError as error:
            raise FeedbackStateError("Feedback kind is invalid") from error

    @classmethod
    def _draft(cls, state: FsmState) -> FeedbackDraft:
        body = state.data.get("body")
        file_id = state.data.get("telegram_file_id")
        return FeedbackDraft(
            kind=cls._kind(state.data),
            body=body if isinstance(body, str) else None,
            telegram_file_id=file_id if isinstance(file_id, str) else None,
            state=state.state,
        )

    @staticmethod
    def _notification(
        ticket: FeedbackTicket,
        user: User,
        body: str,
        telegram_file_id: str | None,
        *,
        continuation: bool = False,
    ) -> FeedbackNotification:
        return FeedbackNotification(
            ticket_id=ticket.id,
            user_id=user.id,
            telegram_id=user.telegram_id,
            kind=ticket.kind,
            display_name=user.display_name,
            username=user.username,
            body=body,
            telegram_file_id=telegram_file_id,
            continuation=continuation,
        )
