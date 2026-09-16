from __future__ import annotations

from datetime import datetime
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
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from terricon_events_bot.domain.enums import (
    Locale,
    SourceTheme,
    SyncEndpointStatus,
    SyncRunStatus,
    SyncTrigger,
)
from terricon_events_bot.infrastructure.database import Base
from terricon_events_bot.infrastructure.models.types import enum_type


class SyncState(Base):
    __tablename__ = "sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default="1")
    baseline_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint("successful_endpoints >= 0", name="successful_nonnegative"),
        CheckConstraint("failed_endpoints >= 0", name="failed_nonnegative"),
        CheckConstraint("records_seen >= 0", name="records_seen_nonnegative"),
        Index("ix_sync_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trigger: Mapped[SyncTrigger] = mapped_column(
        enum_type(SyncTrigger, "sync_trigger"), nullable=False
    )
    status: Mapped[SyncRunStatus] = mapped_column(
        enum_type(SyncRunStatus, "sync_run_status"),
        nullable=False,
        default=SyncRunStatus.RUNNING,
        server_default="running",
    )
    is_baseline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    successful_endpoints: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_endpoints: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncEndpointState(Base):
    __tablename__ = "sync_endpoint_states"
    __table_args__ = (
        CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
        Index("ix_sync_endpoint_states_last_success_at", "last_success_at"),
    )

    locale: Mapped[Locale] = mapped_column(enum_type(Locale, "locale"), primary_key=True)
    source_theme: Mapped[SourceTheme] = mapped_column(
        enum_type(SourceTheme, "source_theme"), primary_key=True
    )
    last_status: Mapped[SyncEndpointStatus | None] = mapped_column(
        enum_type(SyncEndpointStatus, "sync_endpoint_status")
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SyncRunEndpoint(Base):
    __tablename__ = "sync_run_endpoints"
    __table_args__ = (
        UniqueConstraint("sync_run_id", "locale", "source_theme"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("records_seen >= 0", name="records_seen_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    locale: Mapped[Locale] = mapped_column(enum_type(Locale, "locale"), nullable=False)
    source_theme: Mapped[SourceTheme] = mapped_column(
        enum_type(SourceTheme, "source_theme"), nullable=False
    )
    status: Mapped[SyncEndpointStatus] = mapped_column(
        enum_type(SyncEndpointStatus, "sync_endpoint_status"),
        nullable=False,
        default=SyncEndpointStatus.PENDING,
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    records_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
