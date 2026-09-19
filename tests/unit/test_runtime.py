from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from terricon_events_bot.composition import Foundation
from terricon_events_bot.config import Settings
from terricon_events_bot.runtime import RuntimeJobs, _load_runtime_settings, build_scheduler


def settings() -> Settings:
    return Settings(
        _env_file=None,
        BASE_URL="https://api.example.test/events",
        TELEGRAM_BOT_TOKEN="123456:abcdefghijklmnopqrstuvwxyzABCDE12345678",
        OPENAI_API_KEY="openai-secret",
        DATABASE_URL="postgresql+asyncpg://user:pass@db/events",
        ADMIN_TELEGRAM_IDS="100001",
        ACCESS_MODE="allowlist",
        ALLOWED_TELEGRAM_IDS="100001",
    )


def test_scheduler_registers_all_runtime_jobs_without_starting_external_io() -> None:
    jobs = RuntimeJobs(
        cast(Foundation, Mock()),
        cast(Any, Mock()),
        cast(Any, Mock()),
    )

    scheduler = build_scheduler(settings(), jobs)

    assert {job.id for job in scheduler.get_jobs()} == {
        "admin_alerts",
        "broadcasts",
        "classification",
        "feedback_cleanup",
        "heartbeat",
        "notifications",
        "sync",
    }


def test_docker_runtime_files_are_present() -> None:
    root = Path(__file__).resolve().parents[2]

    assert (root / "Dockerfile").is_file()
    assert (root / "compose.yaml").is_file()
    assert (root / ".dockerignore").is_file()


def test_runtime_configuration_error_does_not_print_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "must-not-be-printed"

    def invalid_settings() -> Settings:
        return Settings(
            _env_file=None,
            BASE_URL="not-a-url",
            TELEGRAM_BOT_TOKEN=secret,
            OPENAI_API_KEY=secret,
            DATABASE_URL="not-a-database-url",
            ADMIN_TELEGRAM_IDS="100001",
            ACCESS_MODE="allowlist",
            ALLOWED_TELEGRAM_IDS="100001",
        )

    monkeypatch.setattr("terricon_events_bot.runtime.load_settings", invalid_settings)

    with pytest.raises(SystemExit) as caught:
        _load_runtime_settings()

    assert caught.value.code == 2
    assert secret not in capsys.readouterr().err
