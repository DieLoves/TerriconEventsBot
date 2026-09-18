from pathlib import Path

import pytest

from terricon_events_bot.composition import build_foundation
from terricon_events_bot.config import Settings
from terricon_events_bot.infrastructure.openai_adapter import OpenAIEventAdapter
from terricon_events_bot.infrastructure.terricon import TerriconClient
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


@pytest.mark.asyncio
async def test_foundation_wires_and_closes_terricon_import_services() -> None:
    settings = Settings(
        _env_file=None,
        BASE_URL="https://api.example.test/events",
        TELEGRAM_BOT_TOKEN="bot-secret",
        OPENAI_API_KEY="openai-secret",
        DATABASE_URL="postgresql+asyncpg://user:pass@db/events",
        ADMIN_TELEGRAM_IDS="100001",
        ACCESS_MODE="allowlist",
        ALLOWED_TELEGRAM_IDS="100001",
    )

    foundation = build_foundation(settings, PROJECT_ROOT)
    try:
        assert isinstance(foundation.terricon_client, TerriconClient)
        assert isinstance(foundation.openai_adapter, OpenAIEventAdapter)
        assert foundation.sync_service is not None
        assert foundation.classification_worker is not None
        assert not foundation.http_client.is_closed
        assert not foundation.openai_client.is_closed()
    finally:
        await foundation.close()

    assert foundation.http_client.is_closed
    assert foundation.openai_client.is_closed()
