from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from terricon_events_bot.infrastructure.database import Base


class AIQuotaState(Base):
    __tablename__ = "ai_quota_states"
    __table_args__ = (
        CheckConstraint("remaining_requests >= 0", name="requests_nonnegative"),
        CheckConstraint("remaining_tokens >= 0", name="tokens_nonnegative"),
    )

    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remaining_requests: Mapped[int | None] = mapped_column(BigInteger)
    requests_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remaining_tokens: Mapped[int | None] = mapped_column(BigInteger)
    tokens_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIQuotaReservation(Base):
    __tablename__ = "ai_quota_reservations"
    __table_args__ = (
        CheckConstraint("tokens >= 0", name="tokens_nonnegative"),
        Index("ix_ai_quota_reservations_scope_created_at", "scope", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    scope: Mapped[str] = mapped_column(
        String(128), ForeignKey("ai_quota_states.scope", ondelete="CASCADE"), nullable=False
    )
    tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
