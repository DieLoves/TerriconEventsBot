"""Add restart-safe broadcast delivery state.

Revision ID: 0009_stage7_broadcast_state
Revises: 0008_stage6_delivery_state
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_stage7_broadcast_state"
down_revision: str | None = "0008_stage6_delivery_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "broadcast_deliveries",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "broadcasts",
        sa.Column("report_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "broadcast_deliveries",
        sa.Column(
            "telegram_message_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE broadcast_deliveries "
            "SET telegram_message_ids = ARRAY[telegram_message_id] "
            "WHERE telegram_message_id IS NOT NULL"
        )
    )
    op.create_index(
        "ix_broadcast_deliveries_claimable",
        "broadcast_deliveries",
        ["next_attempt_at", "locked_at"],
        postgresql_where=sa.text("status IN ('pending', 'retry_wait', 'processing')"),
    )


def downgrade() -> None:
    op.drop_index("ix_broadcast_deliveries_claimable", table_name="broadcast_deliveries")
    op.drop_column("broadcast_deliveries", "telegram_message_ids")
    op.drop_column("broadcast_deliveries", "locked_at")
    op.drop_column("broadcasts", "report_sent_at")
