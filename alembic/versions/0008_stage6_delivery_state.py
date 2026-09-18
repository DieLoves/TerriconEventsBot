"""Add durable stage-six delivery queue state.

Revision ID: 0008_stage6_delivery_state
Revises: 0007_openai_cost_precision
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_stage6_delivery_state"
down_revision: str | None = "0007_openai_cost_precision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "domain_changes",
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_domain_changes_unmaterialized",
        "domain_changes",
        ["id"],
        postgresql_where=sa.text("materialized_at IS NULL"),
    )
    op.create_index(
        "ix_notification_outbox_claimable",
        "notification_outbox",
        ["available_at", "locked_at"],
        postgresql_where=sa.text("status IN ('pending', 'retry_wait', 'processing')"),
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_claimable", table_name="notification_outbox")
    op.drop_index("ix_domain_changes_unmaterialized", table_name="domain_changes")
    op.drop_column("domain_changes", "materialized_at")
