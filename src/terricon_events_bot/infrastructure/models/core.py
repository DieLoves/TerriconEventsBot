from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from terricon_events_bot.domain.enums import (
    CategorySlug,
    ClassificationStatus,
    EventFormat,
    EventState,
    Locale,
    SourceTheme,
    TranslationSource,
)
from terricon_events_bot.infrastructure.database import Base
from terricon_events_bot.infrastructure.models.types import enum_type


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("telegram_id > 0", name="telegram_id_positive"),
        Index("ix_users_active_last_active", "is_active", "last_active_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(255))
    locale: Mapped[Locale] = mapped_column(
        enum_type(Locale, "locale"), nullable=False, default=Locale.RU, server_default="ru"
    )
    onboarding_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    menu_images_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    event_posters_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    access_granted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    feedback_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback_blocked_by: Mapped[int | None] = mapped_column(BigInteger)
    current_menu_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    current_menu_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BetaAllowlistEntry(Base):
    __tablename__ = "beta_allowlist"
    __table_args__ = (CheckConstraint("telegram_id > 0", name="telegram_id_positive"),)

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    added_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("source_id > 0", name="source_id_positive"),
        CheckConstraint("missing_streak >= 0", name="missing_streak_nonnegative"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_events_starts_at", "starts_at"),
        Index("ix_events_available_starts_at", "is_available", "starts_at"),
        Index("ix_events_event_languages_gin", "event_languages", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    source_theme: Mapped[SourceTheme] = mapped_column(
        enum_type(SourceTheme, "source_theme"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_format: Mapped[EventFormat] = mapped_column(
        enum_type(EventFormat, "event_format"),
        nullable=False,
        default=EventFormat.UNKNOWN,
        server_default="unknown",
    )
    event_languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    address: Mapped[str | None] = mapped_column(Text)
    registration_url: Mapped[str | None] = mapped_column(Text)
    recording_url: Mapped[str | None] = mapped_column(Text)
    details_url: Mapped[str | None] = mapped_column(Text)
    poster_url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[EventState] = mapped_column(
        enum_type(EventState, "event_state"),
        nullable=False,
        default=EventState.ACTIVE,
        server_default="active",
    )
    is_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    missing_streak: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    semantic_hash: Mapped[str | None] = mapped_column(String(64))
    important_hash: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EventLocalization(Base):
    __tablename__ = "event_localizations"
    __table_args__ = (
        UniqueConstraint("event_id", "locale"),
        Index("ix_event_localizations_locale", "locale"),
    )

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    locale: Mapped[Locale] = mapped_column(enum_type(Locale, "locale"), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(Text)
    translation_source: Mapped[TranslationSource] = mapped_column(
        enum_type(TranslationSource, "translation_source"),
        nullable=False,
        default=TranslationSource.OFFICIAL,
        server_default="official",
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    translated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EventCategory(Base):
    __tablename__ = "event_categories"
    __table_args__ = (
        UniqueConstraint("event_id", "category"),
        Index("ix_event_categories_category_event_id", "category", "event_id"),
    )

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[CategorySlug] = mapped_column(
        enum_type(CategorySlug, "category_slug"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventClassification(Base):
    __tablename__ = "event_classifications"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint("cost_usd >= 0", name="cost_nonnegative"),
        Index("ix_event_classifications_status_next_attempt", "status", "next_attempt_at"),
    )

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[ClassificationStatus] = mapped_column(
        enum_type(ClassificationStatus, "classification_status"),
        nullable=False,
        default=ClassificationStatus.PENDING,
        server_default="pending",
    )
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    result_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
