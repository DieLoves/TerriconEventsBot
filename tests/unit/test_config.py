from datetime import time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from terricon_events_bot.config import Settings
from terricon_events_bot.domain.enums import AccessMode


def valid_values() -> dict[str, object]:
    return {
        "BASE_URL": "https://api.example.test/events",
        "TELEGRAM_BOT_TOKEN": "bot-secret",
        "OPENAI_API_KEY": "openai-secret",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@db/events",
        "ADMIN_TELEGRAM_IDS": "100001, 100002",
        "ACCESS_MODE": "allowlist",
        "ALLOWED_TELEGRAM_IDS": "100003",
    }


def test_settings_parse_ids_defaults_and_aliases() -> None:
    values = valid_values()
    values.pop("TELEGRAM_BOT_TOKEN")
    values["BOT_TOKEN"] = "legacy-name-is-supported"

    settings = Settings(_env_file=None, **values)

    assert settings.admin_telegram_ids == frozenset({100001, 100002})
    assert settings.allowed_telegram_ids == frozenset({100003})
    assert settings.access_mode is AccessMode.ALLOWLIST
    assert settings.quiet_hours_start == time(22)
    assert settings.events_page_size == 5
    assert settings.openai_input_price_per_million_usd == Decimal("0.20")
    assert settings.openai_output_price_per_million_usd == Decimal("1.20")
    assert settings.openai_timeout_seconds == 30
    assert settings.telegram_bot_token.get_secret_value() == "legacy-name-is-supported"


@pytest.mark.parametrize("missing", ["TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "DATABASE_URL"])
def test_required_secrets_and_database_are_validated(missing: str) -> None:
    values = valid_values()
    values.pop(missing)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_allowlist_requires_allowed_ids() -> None:
    values = valid_values()
    values["ALLOWED_TELEGRAM_IDS"] = ""

    with pytest.raises(ValidationError, match="ALLOWED_TELEGRAM_IDS"):
        Settings(_env_file=None, **values)


def test_public_mode_does_not_require_allowlist() -> None:
    values = valid_values()
    values["ACCESS_MODE"] = "public"
    values["ALLOWED_TELEGRAM_IDS"] = ""

    settings = Settings(_env_file=None, **values)

    assert settings.access_mode is AccessMode.PUBLIC


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SYNC_INTERVAL_HOURS", "0"),
        ("SYNC_MAX_RETRIES", "11"),
        ("EVENTS_PAGE_SIZE", "0"),
        ("FEEDBACK_RETENTION_DAYS", "0"),
        ("OPENAI_INPUT_PRICE_PER_MILLION_USD", "-0.01"),
        ("OPENAI_OUTPUT_PRICE_PER_MILLION_USD", "-0.01"),
        ("OPENAI_MONTHLY_BUDGET_USD", "NaN"),
        ("OPENAI_INPUT_PRICE_PER_MILLION_USD", "Infinity"),
        ("OPENAI_TIMEOUT_SECONDS", "0"),
        ("OPENAI_TIMEOUT_SECONDS", "NaN"),
        ("OPENAI_MODEL", "   "),
        ("OPENAI_MODEL", "x" * 129),
        ("ADMIN_TELEGRAM_IDS", "not-an-id"),
        ("DATABASE_URL", "sqlite:///events.db"),
        ("APP_TIMEZONE", "Mars/Olympus"),
        ("LOG_LEVEL", "VERBOSE"),
    ],
)
def test_invalid_configuration_is_rejected(name: str, value: str) -> None:
    values = valid_values()
    values[name] = value

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_secret_representation_does_not_reveal_values() -> None:
    settings = Settings(_env_file=None, **valid_values())

    representation = repr(settings)

    assert "bot-secret" not in representation
    assert "openai-secret" not in representation
    assert "user:pass" not in representation
    assert "100001" not in representation
    assert "100003" not in representation
