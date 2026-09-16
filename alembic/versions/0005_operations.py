"""Create OpenAI usage, alert deduplication and heartbeat tables.

Revision ID: 0005_operations
Revises: 0004_feedback_broadcasts
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_operations"
down_revision: str | None = "0004_feedback_broadcasts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def db_enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    op.create_table(
        "openai_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("event_id", sa.BigInteger()),
        sa.Column(
            "operation",
            db_enum("openai_operation", "classification", "translation"),
            nullable=False,
        ),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        sa.CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        sa.CheckConstraint("cost_usd >= 0", name="cost_nonnegative"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            ondelete="SET NULL",
            name="fk_openai_usage_event_id_events",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_openai_usage"),
    )
    op.create_index("ix_openai_usage_occurred_at", "openai_usage", ["occurred_at"])

    op.create_table(
        "admin_alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("occurrence_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_notified_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("occurrence_count >= 1", name="occurrence_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_admin_alerts"),
    )
    op.create_index(
        "uq_admin_alerts_active_fingerprint",
        "admin_alerts",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index("ix_admin_alerts_last_seen_at", "admin_alerts", ["last_seen_at"])

    op.create_table(
        "app_heartbeats",
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "details",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name="pk_app_heartbeats"),
    )


def downgrade() -> None:
    op.drop_table("app_heartbeats")
    op.drop_table("admin_alerts")
    op.drop_table("openai_usage")
