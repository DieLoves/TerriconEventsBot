from pathlib import Path

import pytest

from terricon_events_bot.composition import build_foundation
from terricon_events_bot.config import Settings
from terricon_events_bot.localization import LocalizationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_public_foundation_rejects_unfinished_kz_locale() -> None:
    settings = Settings(
        _env_file=None,
        BASE_URL="https://api.example.test/events",
        TELEGRAM_BOT_TOKEN="bot-secret",
        OPENAI_API_KEY="openai-secret",
        DATABASE_URL="postgresql+asyncpg://user:pass@db/events",
        ADMIN_TELEGRAM_IDS="100001",
        ACCESS_MODE="public",
        ALLOWED_TELEGRAM_IDS="",
    )

    with pytest.raises(LocalizationError, match="Public KZ mode"):
        build_foundation(settings, PROJECT_ROOT)
