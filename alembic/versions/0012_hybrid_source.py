"""Track the hybrid event source, event types and program subscriptions.

Revision ID: 0012_hybrid_source
Revises: 0011_groq_shared_quota
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_hybrid_source"
down_revision: str | None = "0011_groq_shared_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SYNC_SOURCE = postgresql.ENUM("stage", "legacy", name="sync_source", create_type=False)
_THEME_MATCHES_SOURCE = "(source = 'stage') = (source_theme IS NULL)"


def upgrade() -> None:
    _SYNC_SOURCE.create(op.get_bind(), checkfirst=False)
    op.add_column("events", sa.Column("event_type", sa.String(64)))
    op.add_column(
        "users",
        sa.Column(
            "program_notifications", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column("sync_runs", sa.Column("source", _SYNC_SOURCE))
    op.add_column("sync_state", sa.Column("fallback_since", sa.DateTime(timezone=True)))

    op.add_column(
        "sync_endpoint_states",
        sa.Column("source", _SYNC_SOURCE, server_default="legacy", nullable=False),
    )
    op.drop_constraint("pk_sync_endpoint_states", "sync_endpoint_states", type_="primary")
    op.add_column(
        "sync_endpoint_states",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
    )
    op.create_primary_key("pk_sync_endpoint_states", "sync_endpoint_states", ["id"])
    op.alter_column("sync_endpoint_states", "source_theme", nullable=True)
    op.create_unique_constraint(
        "uq_sync_endpoint_states_source_locale_source_theme",
        "sync_endpoint_states",
        ["source", "locale", "source_theme"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_check_constraint(
        "ck_sync_endpoint_states_theme_matches_source",
        "sync_endpoint_states",
        _THEME_MATCHES_SOURCE,
    )

    op.add_column(
        "sync_run_endpoints",
        sa.Column("source", _SYNC_SOURCE, server_default="legacy", nullable=False),
    )
    op.drop_constraint(
        "uq_sync_run_endpoints_sync_run_id_locale_source_theme",
        "sync_run_endpoints",
        type_="unique",
    )
    op.alter_column("sync_run_endpoints", "source_theme", nullable=True)
    op.create_unique_constraint(
        "uq_sync_run_endpoints_sync_run_id_source_locale_source_theme",
        "sync_run_endpoints",
        ["sync_run_id", "source", "locale", "source_theme"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_check_constraint(
        "ck_sync_run_endpoints_theme_matches_source",
        "sync_run_endpoints",
        _THEME_MATCHES_SOURCE,
    )


def downgrade() -> None:
    op.execute("DELETE FROM sync_run_endpoints WHERE source = 'stage'")
    op.drop_constraint(
        "ck_sync_run_endpoints_theme_matches_source", "sync_run_endpoints", type_="check"
    )
    op.drop_constraint(
        "uq_sync_run_endpoints_sync_run_id_source_locale_source_theme",
        "sync_run_endpoints",
        type_="unique",
    )
    op.alter_column("sync_run_endpoints", "source_theme", nullable=False)
    op.create_unique_constraint(
        "uq_sync_run_endpoints_sync_run_id_locale_source_theme",
        "sync_run_endpoints",
        ["sync_run_id", "locale", "source_theme"],
    )
    op.drop_column("sync_run_endpoints", "source")

    op.execute("DELETE FROM sync_endpoint_states WHERE source = 'stage'")
    op.drop_constraint(
        "ck_sync_endpoint_states_theme_matches_source", "sync_endpoint_states", type_="check"
    )
    op.drop_constraint(
        "uq_sync_endpoint_states_source_locale_source_theme",
        "sync_endpoint_states",
        type_="unique",
    )
    op.drop_constraint("pk_sync_endpoint_states", "sync_endpoint_states", type_="primary")
    op.drop_column("sync_endpoint_states", "id")
    op.alter_column("sync_endpoint_states", "source_theme", nullable=False)
    op.create_primary_key(
        "pk_sync_endpoint_states", "sync_endpoint_states", ["locale", "source_theme"]
    )
    op.drop_column("sync_endpoint_states", "source")

    op.drop_column("sync_state", "fallback_since")
    op.drop_column("sync_runs", "source")
    op.drop_column("users", "program_notifications")
    op.drop_column("events", "event_type")
    _SYNC_SOURCE.drop(op.get_bind(), checkfirst=False)
