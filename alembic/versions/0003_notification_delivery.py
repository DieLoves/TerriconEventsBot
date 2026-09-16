"""Create durable domain changes, deliveries and outbox.

Revision ID: 0003_notification_delivery
Revises: 0002_sync_subscriptions
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_notification_delivery"
down_revision: str | None = "0002_sync_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def db_enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    op.create_table(
        "domain_changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("event_revision", sa.Integer(), nullable=False),
        sa.Column(
            "notification_type",
            db_enum("notification_type", "new_event", "important_change", "cancellation"),
            nullable=False,
        ),
        sa.Column(
            "old_categories",
            postgresql.ARRAY(sa.String(64)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column(
            "new_categories",
            postgresql.ARRAY(sa.String(64)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column(
            "change_data",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("event_revision >= 1", name="revision_positive"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            ondelete="CASCADE",
            name="fk_domain_changes_event_id_events",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_domain_changes"),
        sa.UniqueConstraint(
            "event_id",
            "event_revision",
            "notification_type",
            name="uq_domain_changes_event_id_event_revision_notification_type",
        ),
    )
    op.create_index("ix_domain_changes_created_at", "domain_changes", ["created_at"])

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_key", sa.String(255), nullable=False),
        sa.Column(
            "status",
            db_enum(
                "delivery_status",
                "pending",
                "processing",
                "sent",
                "retry_wait",
                "failed",
                "blocked",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column(
            "telegram_message_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_notification_outbox_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_outbox"),
        sa.UniqueConstraint("batch_key", name="uq_notification_outbox_batch_key"),
    )
    op.create_index(
        "ix_notification_outbox_pending_available",
        "notification_outbox",
        ["available_at"],
        postgresql_where=sa.text("status IN ('pending', 'retry_wait')"),
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("domain_change_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("outbox_id", sa.BigInteger()),
        sa.Column("event_revision", sa.Integer(), nullable=False),
        sa.Column(
            "notification_type",
            db_enum("notification_type", "new_event", "important_change", "cancellation"),
            nullable=False,
        ),
        sa.Column(
            "status",
            db_enum(
                "delivery_status",
                "pending",
                "processing",
                "sent",
                "retry_wait",
                "failed",
                "blocked",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("event_revision >= 1", name="revision_positive"),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["domain_change_id"],
            ["domain_changes.id"],
            ondelete="CASCADE",
            name="fk_notification_deliveries_domain_change_id_domain_changes",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            ondelete="CASCADE",
            name="fk_notification_deliveries_event_id_events",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["notification_outbox.id"],
            ondelete="SET NULL",
            name="fk_notification_deliveries_outbox_id_notification_outbox",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_notification_deliveries_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_deliveries"),
        sa.UniqueConstraint(
            "user_id",
            "event_id",
            "event_revision",
            "notification_type",
            name="uq_notification_deliveries_idempotency",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_status_next_attempt",
        "notification_deliveries",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("notification_outbox")
    op.drop_table("domain_changes")
