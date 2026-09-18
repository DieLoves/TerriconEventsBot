from alembic.config import Config
from alembic.script import ScriptDirectory

from terricon_events_bot.infrastructure import models
from terricon_events_bot.infrastructure.database import Base

_ = models

EXPECTED_TABLES = {
    "admin_alerts",
    "app_heartbeats",
    "beta_allowlist",
    "broadcast_deliveries",
    "broadcast_localizations",
    "broadcasts",
    "domain_changes",
    "event_categories",
    "event_classifications",
    "event_localizations",
    "events",
    "feedback_messages",
    "feedback_tickets",
    "fsm_states",
    "notification_deliveries",
    "notification_outbox",
    "openai_usage",
    "subscription_categories",
    "subscription_settings",
    "sync_endpoint_states",
    "sync_run_endpoints",
    "sync_runs",
    "sync_state",
    "users",
}


def test_metadata_contains_complete_stage_two_schema() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_critical_indexes_are_declared() -> None:
    index_names = {index.name for table in Base.metadata.tables.values() for index in table.indexes}

    assert {
        "ix_events_starts_at",
        "ix_events_available_starts_at",
        "ix_event_categories_category_event_id",
        "ix_notification_outbox_pending_available",
        "ix_feedback_tickets_open_updated_at",
        "ix_broadcasts_unfinished_updated_at",
    } <= index_names


def test_delivery_idempotency_constraint_has_required_dimensions() -> None:
    table = Base.metadata.tables["notification_deliveries"]
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("user_id", "event_id", "event_revision", "notification_type") in unique_column_sets


def test_subscription_mode_is_enforced_by_composite_foreign_key() -> None:
    table = Base.metadata.tables["subscription_categories"]
    foreign_keys = {
        tuple(element.parent.name for element in constraint.elements): tuple(
            element.target_fullname for element in constraint.elements
        )
        for constraint in table.foreign_key_constraints
    }

    assert foreign_keys[("user_id", "subscribe_all")] == (
        "subscription_settings.user_id",
        "subscription_settings.subscribe_all",
    )


def test_alembic_history_is_linear_and_has_expected_head() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    revisions = list(scripts.walk_revisions(base="base", head="heads"))

    assert scripts.get_heads() == ["0006_localized_details_url"]
    assert [revision.revision for revision in reversed(revisions)] == [
        "0001_core",
        "0002_sync_subscriptions",
        "0003_notification_delivery",
        "0004_feedback_broadcasts",
        "0005_operations",
        "0006_localized_details_url",
    ]


def test_details_url_belongs_to_event_localization() -> None:
    events = Base.metadata.tables["events"]
    localizations = Base.metadata.tables["event_localizations"]

    assert "details_url" not in events.c
    assert "details_url" in localizations.c
