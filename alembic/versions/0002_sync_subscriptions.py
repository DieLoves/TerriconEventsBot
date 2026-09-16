"""Create synchronization state and subscriptions.

Revision ID: 0002_sync_subscriptions
Revises: 0001_core
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_sync_subscriptions"
down_revision: str | None = "0001_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def db_enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    op.create_table(
        "sync_state",
        sa.Column("id", sa.Integer(), server_default="1", nullable=False),
        sa.Column("baseline_completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_full_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="singleton"),
        sa.PrimaryKeyConstraint("id", name="pk_sync_state"),
    )
    op.execute(sa.text("INSERT INTO sync_state (id) VALUES (1)"))

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "trigger",
            db_enum("sync_trigger", "startup", "scheduled", "manual"),
            nullable=False,
        ),
        sa.Column(
            "status",
            db_enum("sync_run_status", "running", "succeeded", "partial", "failed"),
            server_default="running",
            nullable=False,
        ),
        sa.Column("is_baseline", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("successful_endpoints", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_endpoints", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "error_summary",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("successful_endpoints >= 0", name="successful_nonnegative"),
        sa.CheckConstraint("failed_endpoints >= 0", name="failed_nonnegative"),
        sa.CheckConstraint("records_seen >= 0", name="records_seen_nonnegative"),
        sa.PrimaryKeyConstraint("id", name="pk_sync_runs"),
    )
    op.create_index("ix_sync_runs_started_at", "sync_runs", ["started_at"])

    op.create_table(
        "sync_endpoint_states",
        sa.Column("locale", db_enum("locale", "ru", "kz"), nullable=False),
        sa.Column(
            "source_theme",
            db_enum("source_theme", "it", "business", "marketing"),
            nullable=False,
        ),
        sa.Column(
            "last_status",
            db_enum("sync_endpoint_status", "pending", "succeeded", "failed"),
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
        sa.PrimaryKeyConstraint("locale", "source_theme", name="pk_sync_endpoint_states"),
    )
    op.create_index(
        "ix_sync_endpoint_states_last_success_at", "sync_endpoint_states", ["last_success_at"]
    )

    op.create_table(
        "sync_run_endpoints",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=False),
        sa.Column("locale", db_enum("locale", "ru", "kz"), nullable=False),
        sa.Column(
            "source_theme",
            db_enum("source_theme", "it", "business", "marketing"),
            nullable=False,
        ),
        sa.Column(
            "status",
            db_enum("sync_endpoint_status", "pending", "succeeded", "failed"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("records_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column(
            "error_details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        sa.CheckConstraint("records_seen >= 0", name="records_seen_nonnegative"),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.id"],
            ondelete="CASCADE",
            name="fk_sync_run_endpoints_sync_run_id_sync_runs",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_run_endpoints"),
        sa.UniqueConstraint(
            "sync_run_id",
            "locale",
            "source_theme",
            name="uq_sync_run_endpoints_sync_run_id_locale_source_theme",
        ),
    )

    op.create_table(
        "subscription_settings",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("subscribe_all", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_subscription_settings_user_id_users",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_subscription_settings"),
        sa.UniqueConstraint(
            "user_id",
            "subscribe_all",
            name="uq_subscription_settings_user_id_subscribe_all",
        ),
    )

    op.create_table(
        "subscription_categories",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
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
        sa.Column("subscribe_all", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("subscribe_all = false", name="requires_category_mode"),
        sa.ForeignKeyConstraint(
            ["user_id", "subscribe_all"],
            ["subscription_settings.user_id", "subscription_settings.subscribe_all"],
            ondelete="CASCADE",
            name="fk_subscription_categories_settings_mode",
        ),
        sa.PrimaryKeyConstraint("user_id", "category", name="pk_subscription_categories"),
    )
    op.create_index(
        "ix_subscription_categories_category_user_id",
        "subscription_categories",
        ["category", "user_id"],
    )


def downgrade() -> None:
    op.drop_table("subscription_categories")
    op.drop_table("subscription_settings")
    op.drop_table("sync_run_endpoints")
    op.drop_table("sync_endpoint_states")
    op.drop_table("sync_runs")
    op.drop_table("sync_state")
