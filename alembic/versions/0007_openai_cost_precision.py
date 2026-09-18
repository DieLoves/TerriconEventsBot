"""Increase OpenAI cost precision for sub-microdollar token charges.

Revision ID: 0007_openai_cost_precision
Revises: 0006_localized_details_url
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_openai_cost_precision"
down_revision: str | None = "0006_localized_details_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "event_classifications",
        "cost_usd",
        existing_type=sa.Numeric(12, 6),
        type_=sa.Numeric(18, 9),
        existing_nullable=False,
    )
    op.alter_column(
        "openai_usage",
        "cost_usd",
        existing_type=sa.Numeric(12, 6),
        type_=sa.Numeric(18, 9),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "openai_usage",
        "cost_usd",
        existing_type=sa.Numeric(18, 9),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
    )
    op.alter_column(
        "event_classifications",
        "cost_usd",
        existing_type=sa.Numeric(18, 9),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
    )
