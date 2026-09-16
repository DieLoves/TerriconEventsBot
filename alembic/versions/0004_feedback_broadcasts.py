"""Create feedback, persistent FSM and broadcasts.

Revision ID: 0004_feedback_broadcasts
Revises: 0003_notification_delivery
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_feedback_broadcasts"
down_revision: str | None = "0003_notification_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def db_enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    op.create_table(
        "feedback_tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            db_enum("feedback_kind", "error", "idea", "other"),
            nullable=False,
        ),
        sa.Column(
            "status",
            db_enum("ticket_status", "open", "awaiting_admin", "awaiting_user", "closed"),
            server_default="open",
            nullable=False,
        ),
        sa.Column("admin_chat_id", sa.BigInteger()),
        sa.Column("admin_message_id", sa.BigInteger()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
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
            name="fk_feedback_tickets_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_feedback_tickets"),
    )
    op.create_index(
        "ix_feedback_tickets_user_created_at", "feedback_tickets", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_feedback_tickets_open_updated_at",
        "feedback_tickets",
        ["updated_at"],
        postgresql_where=sa.text("status <> 'closed'"),
    )

    op.create_table(
        "feedback_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "author",
            db_enum("feedback_author", "user", "admin"),
            nullable=False,
        ),
        sa.Column("body", sa.Text()),
        sa.Column("telegram_file_id", sa.String(255)),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "body IS NOT NULL OR telegram_file_id IS NOT NULL",
            name="has_content",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["feedback_tickets.id"],
            ondelete="CASCADE",
            name="fk_feedback_messages_ticket_id_feedback_tickets",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_feedback_messages"),
    )
    op.create_index(
        "ix_feedback_messages_ticket_created_at",
        "feedback_messages",
        ["ticket_id", "created_at"],
    )

    op.create_table(
        "fsm_states",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("state", sa.String(128), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
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
            name="fk_fsm_states_user_id_users",
        ),
        sa.PrimaryKeyConstraint("user_id", "scope", name="pk_fsm_states"),
    )

    op.create_table(
        "broadcasts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            db_enum(
                "broadcast_status", "draft", "ready", "running", "completed", "cancelled", "failed"
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "audience",
            db_enum("broadcast_audience", "ru", "kz", "both"),
            nullable=False,
        ),
        sa.Column("telegram_file_id", sa.String(255)),
        sa.Column("total_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("total_count >= 0", name="total_nonnegative"),
        sa.CheckConstraint("sent_count >= 0", name="sent_nonnegative"),
        sa.CheckConstraint("failed_count >= 0", name="failed_nonnegative"),
        sa.PrimaryKeyConstraint("id", name="pk_broadcasts"),
    )
    op.create_index(
        "ix_broadcasts_unfinished_updated_at",
        "broadcasts",
        ["updated_at"],
        postgresql_where=sa.text("status IN ('ready', 'running')"),
    )

    op.create_table(
        "broadcast_localizations",
        sa.Column("broadcast_id", sa.BigInteger(), nullable=False),
        sa.Column("locale", db_enum("locale", "ru", "kz"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["broadcasts.id"],
            ondelete="CASCADE",
            name="fk_broadcast_localizations_broadcast_id_broadcasts",
        ),
        sa.PrimaryKeyConstraint("broadcast_id", "locale", name="pk_broadcast_localizations"),
    )

    op.create_table(
        "broadcast_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("broadcast_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
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
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["broadcasts.id"],
            ondelete="CASCADE",
            name="fk_broadcast_deliveries_broadcast_id_broadcasts",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_broadcast_deliveries_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_broadcast_deliveries"),
        sa.UniqueConstraint(
            "broadcast_id", "user_id", name="uq_broadcast_deliveries_broadcast_id_user_id"
        ),
    )
    op.create_index(
        "ix_broadcast_deliveries_status_next_attempt",
        "broadcast_deliveries",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_table("broadcast_deliveries")
    op.drop_table("broadcast_localizations")
    op.drop_table("broadcasts")
    op.drop_table("fsm_states")
    op.drop_table("feedback_messages")
    op.drop_table("feedback_tickets")
