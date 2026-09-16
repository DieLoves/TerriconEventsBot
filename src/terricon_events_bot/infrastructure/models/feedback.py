from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from terricon_events_bot.domain.enums import FeedbackAuthor, FeedbackKind, TicketStatus
from terricon_events_bot.infrastructure.database import Base
from terricon_events_bot.infrastructure.models.types import enum_type


class FeedbackTicket(Base):
    __tablename__ = "feedback_tickets"
    __table_args__ = (
        Index("ix_feedback_tickets_user_created_at", "user_id", "created_at"),
        Index(
            "ix_feedback_tickets_open_updated_at",
            "updated_at",
            postgresql_where=text("status <> 'closed'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[FeedbackKind] = mapped_column(
        enum_type(FeedbackKind, "feedback_kind"), nullable=False
    )
    status: Mapped[TicketStatus] = mapped_column(
        enum_type(TicketStatus, "ticket_status"),
        nullable=False,
        default=TicketStatus.OPEN,
        server_default="open",
    )
    admin_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_message_id: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class FeedbackMessage(Base):
    __tablename__ = "feedback_messages"
    __table_args__ = (
        CheckConstraint("body IS NOT NULL OR telegram_file_id IS NOT NULL", name="has_content"),
        Index("ix_feedback_messages_ticket_created_at", "ticket_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("feedback_tickets.id", ondelete="CASCADE"), nullable=False
    )
    author: Mapped[FeedbackAuthor] = mapped_column(
        enum_type(FeedbackAuthor, "feedback_author"), nullable=False
    )
    body: Mapped[str | None] = mapped_column(Text)
    telegram_file_id: Mapped[str | None] = mapped_column(String(255))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FsmState(Base):
    __tablename__ = "fsm_states"
    __table_args__ = (UniqueConstraint("user_id", "scope"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
