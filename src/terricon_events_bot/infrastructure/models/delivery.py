from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from terricon_events_bot.domain.enums import DeliveryStatus, NotificationType
from terricon_events_bot.infrastructure.database import Base
from terricon_events_bot.infrastructure.models.types import enum_type


class DomainChange(Base):
    __tablename__ = "domain_changes"
    __table_args__ = (
        CheckConstraint("event_revision >= 1", name="revision_positive"),
        UniqueConstraint("event_id", "event_revision", "notification_type"),
        Index("ix_domain_changes_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    event_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    notification_type: Mapped[NotificationType] = mapped_column(
        enum_type(NotificationType, "notification_type"), nullable=False
    )
    old_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    new_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    change_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        Index(
            "ix_notification_outbox_pending_available",
            "available_at",
            postgresql_where=text("status IN ('pending', 'retry_wait')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    batch_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_type(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
        server_default="pending",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    telegram_message_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list, server_default=text("'{}'::bigint[]")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint("event_revision >= 1", name="revision_positive"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        UniqueConstraint(
            "user_id",
            "event_id",
            "event_revision",
            "notification_type",
            name="uq_notification_deliveries_idempotency",
        ),
        Index("ix_notification_deliveries_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    domain_change_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("domain_changes.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("notification_outbox.id", ondelete="SET NULL")
    )
    event_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    notification_type: Mapped[NotificationType] = mapped_column(
        enum_type(NotificationType, "notification_type"), nullable=False
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_type(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
