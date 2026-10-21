"""Add report leasing and queue access indexes.

Revision ID: 0010_hardening_queue_state
Revises: 0009_stage7_broadcast_state
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_hardening_queue_state"
down_revision: str | None = "0009_stage7_broadcast_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "broadcasts",
        sa.Column("report_locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_broadcasts_pending_report",
        "broadcasts",
        ["report_locked_at", "id"],
        postgresql_where=sa.text("status = 'completed' AND report_sent_at IS NULL"),
    )
    op.create_index(
        "ix_broadcast_deliveries_active_user_id",
        "broadcast_deliveries",
        ["user_id"],
        postgresql_where=sa.text("status IN ('pending', 'retry_wait', 'processing')"),
    )
    op.create_index(
        "ix_notification_outbox_active_user_id",
        "notification_outbox",
        ["user_id"],
        postgresql_where=sa.text("status IN ('pending', 'retry_wait', 'processing')"),
    )
    op.create_index(
        "ix_notification_deliveries_outbox_id_status",
        "notification_deliveries",
        ["outbox_id", "status"],
    )
    op.create_index(
        "ix_notification_deliveries_unbundled_user_id_id",
        "notification_deliveries",
        ["user_id", "id"],
        postgresql_where=sa.text("status = 'pending' AND outbox_id IS NULL"),
    )
    op.create_index(
        "ix_feedback_tickets_closed_at",
        "feedback_tickets",
        ["closed_at"],
        postgresql_where=sa.text("status = 'closed'"),
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_tickets_closed_at", table_name="feedback_tickets")
    op.drop_index(
        "ix_notification_deliveries_unbundled_user_id_id",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "ix_notification_deliveries_outbox_id_status",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "ix_notification_outbox_active_user_id",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_broadcast_deliveries_active_user_id",
        table_name="broadcast_deliveries",
    )
    op.drop_index("ix_broadcasts_pending_report", table_name="broadcasts")
    op.drop_column("broadcasts", "report_locked_at")
