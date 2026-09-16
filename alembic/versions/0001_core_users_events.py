"""Create users, events, localizations and classification queue.

Revision ID: 0001_core
Revises:
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = (
    postgresql.ENUM("ru", "kz", name="locale"),
    postgresql.ENUM("it", "business", "marketing", name="source_theme"),
    postgresql.ENUM(
        "development_it",
        "ai_data",
        "business_finance",
        "marketing_sales",
        "design_creative",
        "startups_products",
        "career_education_hr",
        "soft_skills_languages",
        "other",
        name="category_slug",
    ),
    postgresql.ENUM("online", "offline", "hybrid", "unknown", name="event_format"),
    postgresql.ENUM("active", "hidden", "cancelled", name="event_state"),
    postgresql.ENUM("official", "machine", name="translation_source"),
    postgresql.ENUM("pending", "completed", "retry_wait", "fallback", name="classification_status"),
    postgresql.ENUM("startup", "scheduled", "manual", name="sync_trigger"),
    postgresql.ENUM("running", "succeeded", "partial", "failed", name="sync_run_status"),
    postgresql.ENUM("pending", "succeeded", "failed", name="sync_endpoint_status"),
    postgresql.ENUM("new_event", "important_change", "cancellation", name="notification_type"),
    postgresql.ENUM(
        "pending",
        "processing",
        "sent",
        "retry_wait",
        "failed",
        "blocked",
        name="delivery_status",
    ),
    postgresql.ENUM("error", "idea", "other", name="feedback_kind"),
    postgresql.ENUM("user", "admin", name="feedback_author"),
    postgresql.ENUM("open", "awaiting_admin", "awaiting_user", "closed", name="ticket_status"),
    postgresql.ENUM(
        "draft", "ready", "running", "completed", "cancelled", "failed", name="broadcast_status"
    ),
    postgresql.ENUM("ru", "kz", "both", name="broadcast_audience"),
    postgresql.ENUM("classification", "translation", name="openai_operation"),
)


def db_enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=False)

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64)),
        sa.Column("display_name", sa.String(255)),
        sa.Column("locale", db_enum("locale", "ru", "kz"), server_default="ru", nullable=False),
        sa.Column("onboarding_completed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("menu_images_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("event_posters_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("access_granted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("feedback_blocked_at", sa.DateTime(timezone=True)),
        sa.Column("feedback_blocked_by", sa.BigInteger()),
        sa.Column("current_menu_chat_id", sa.BigInteger()),
        sa.Column("current_menu_message_id", sa.BigInteger()),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("telegram_id > 0", name="telegram_id_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
    )
    op.create_index("ix_users_active_last_active", "users", ["is_active", "last_active_at"])

    op.create_table(
        "beta_allowlist",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("added_by_telegram_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("telegram_id > 0", name="telegram_id_positive"),
        sa.PrimaryKeyConstraint("telegram_id", name="pk_beta_allowlist"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_theme",
            db_enum("source_theme", "it", "business", "marketing"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "event_format",
            db_enum("event_format", "online", "offline", "hybrid", "unknown"),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column(
            "event_languages",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("address", sa.Text()),
        sa.Column("registration_url", sa.Text()),
        sa.Column("recording_url", sa.Text()),
        sa.Column("details_url", sa.Text()),
        sa.Column("poster_url", sa.Text()),
        sa.Column(
            "state",
            db_enum("event_state", "active", "hidden", "cancelled"),
            server_default="active",
            nullable=False,
        ),
        sa.Column("is_available", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("missing_streak", sa.Integer(), server_default="0", nullable=False),
        sa.Column("semantic_hash", sa.String(64)),
        sa.Column("important_hash", sa.String(64)),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("source_id > 0", name="source_id_positive"),
        sa.CheckConstraint("missing_streak >= 0", name="missing_streak_nonnegative"),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("source_id", name="uq_events_source_id"),
    )
    op.create_index("ix_events_starts_at", "events", ["starts_at"])
    op.create_index("ix_events_available_starts_at", "events", ["is_available", "starts_at"])
    op.create_index(
        "ix_events_event_languages_gin", "events", ["event_languages"], postgresql_using="gin"
    )

    op.create_table(
        "event_localizations",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("locale", db_enum("locale", "ru", "kz"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("audience", sa.Text()),
        sa.Column("speaker", sa.Text()),
        sa.Column(
            "translation_source",
            db_enum("translation_source", "official", "machine"),
            server_default="official",
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("translated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            ondelete="CASCADE",
            name="fk_event_localizations_event_id_events",
        ),
        sa.PrimaryKeyConstraint("event_id", "locale", name="pk_event_localizations"),
    )
    op.create_index("ix_event_localizations_locale", "event_localizations", ["locale"])

    op.create_table(
        "event_categories",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "category",
            db_enum(
                "category_slug",
                "development_it",
                "ai_data",
                "business_finance",
                "marketing_sales",
                "design_creative",
                "startups_products",
                "career_education_hr",
                "soft_skills_languages",
                "other",
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            ondelete="CASCADE",
            name="fk_event_categories_event_id_events",
        ),
        sa.PrimaryKeyConstraint("event_id", "category", name="pk_event_categories"),
    )
    op.create_index(
        "ix_event_categories_category_event_id", "event_categories", ["category", "event_id"]
    )

    op.create_table(
        "event_classifications",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            db_enum("classification_status", "pending", "completed", "retry_wait", "fallback"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("classified_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column(
            "result_metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        sa.CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        sa.CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        sa.CheckConstraint("cost_usd >= 0", name="cost_nonnegative"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            ondelete="CASCADE",
            name="fk_event_classifications_event_id_events",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_event_classifications"),
    )
    op.create_index(
        "ix_event_classifications_status_next_attempt",
        "event_classifications",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("event_classifications")
    op.drop_table("event_categories")
    op.drop_table("event_localizations")
    op.drop_table("events")
    op.drop_table("beta_allowlist")
    op.drop_table("users")
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=False)
