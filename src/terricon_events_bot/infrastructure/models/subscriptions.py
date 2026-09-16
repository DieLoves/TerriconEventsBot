from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from terricon_events_bot.domain.enums import CategorySlug
from terricon_events_bot.infrastructure.database import Base
from terricon_events_bot.infrastructure.models.types import enum_type


class SubscriptionSettings(Base):
    __tablename__ = "subscription_settings"
    __table_args__ = (UniqueConstraint("user_id", "subscribe_all"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    subscribe_all: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SubscriptionCategory(Base):
    __tablename__ = "subscription_categories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "subscribe_all"],
            ["subscription_settings.user_id", "subscription_settings.subscribe_all"],
            ondelete="CASCADE",
            name="fk_subscription_categories_settings_mode",
        ),
        CheckConstraint("subscribe_all = false", name="requires_category_mode"),
        Index("ix_subscription_categories_category_user_id", "category", "user_id"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category: Mapped[CategorySlug] = mapped_column(
        enum_type(CategorySlug, "category_slug"), primary_key=True
    )
    subscribe_all: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
