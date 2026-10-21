"""Persist shared Groq quotas before external requests.

Revision ID: 0011_groq_shared_quota
Revises: 0010_hardening_queue_state
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_groq_shared_quota"
down_revision: str | None = "0010_hardening_queue_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_quota_states",
        sa.Column("scope", sa.String(128), primary_key=True),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column("remaining_requests", sa.BigInteger()),
        sa.Column("requests_reset_at", sa.DateTime(timezone=True)),
        sa.Column("remaining_tokens", sa.BigInteger()),
        sa.Column("tokens_reset_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "remaining_requests >= 0", name=op.f("ck_ai_quota_states_requests_nonnegative")
        ),
        sa.CheckConstraint(
            "remaining_tokens >= 0", name=op.f("ck_ai_quota_states_tokens_nonnegative")
        ),
    )
    op.create_table(
        "ai_quota_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "scope",
            sa.String(128),
            sa.ForeignKey("ai_quota_states.scope", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("tokens >= 0", name=op.f("ck_ai_quota_reservations_tokens_nonnegative")),
    )
    op.create_index(
        "ix_ai_quota_reservations_scope_created_at",
        "ai_quota_reservations",
        ["scope", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("ai_quota_reservations")
    op.drop_table("ai_quota_states")
