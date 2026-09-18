"""Move the source details URL to event localizations.

Revision ID: 0006_localized_details_url
Revises: 0005_operations
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_localized_details_url"
down_revision: str | None = "0005_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("event_localizations", sa.Column("details_url", sa.Text()))
    op.execute(
        sa.text(
            """
            UPDATE event_localizations AS localization
            SET details_url = event.details_url
            FROM events AS event
            WHERE localization.event_id = event.id
              AND event.details_url IS NOT NULL
            """
        )
    )
    op.drop_column("events", "details_url")


def downgrade() -> None:
    op.add_column("events", sa.Column("details_url", sa.Text()))
    op.execute(
        sa.text(
            """
            UPDATE events AS event
            SET details_url = COALESCE(
                (
                    SELECT localization.details_url
                    FROM event_localizations AS localization
                    WHERE localization.event_id = event.id
                      AND localization.locale = 'ru'
                ),
                (
                    SELECT localization.details_url
                    FROM event_localizations AS localization
                    WHERE localization.event_id = event.id
                      AND localization.locale = 'kz'
                )
            )
            WHERE EXISTS (
                SELECT 1
                FROM event_localizations AS localization
                WHERE localization.event_id = event.id
                  AND localization.details_url IS NOT NULL
            )
            """
        )
    )
    op.drop_column("event_localizations", "details_url")
