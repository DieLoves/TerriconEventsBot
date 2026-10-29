"""Subscribe existing users without any subscription to all events.

Revision ID: 0013_default_subscriptions
Revises: 0012_hybrid_source
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_default_subscriptions"
down_revision: str | None = "0012_hybrid_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO subscription_settings (user_id, subscribe_all) "
        "SELECT id, true FROM users "
        "WHERE NOT EXISTS (SELECT 1 FROM subscription_settings s WHERE s.user_id = users.id)"
    )


def downgrade() -> None:
    # Data-only migration: defaulted rows are indistinguishable from user choices.
    pass
