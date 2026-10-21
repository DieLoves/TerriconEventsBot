from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from sqlalchemy.exc import SQLAlchemyError

from terricon_events_bot.composition import Foundation
from terricon_events_bot.config import Settings
from terricon_events_bot.domain.enums import SyncTrigger
from terricon_events_bot.runtime import RuntimeJobs, _load_runtime_settings, build_scheduler


def settings() -> Settings:
    return Settings(
        _env_file=None,
        BASE_URL="https://api.example.test/events",
        TELEGRAM_BOT_TOKEN="123456:abcdefghijklmnopqrstuvwxyzABCDE12345678",
        GROQ_API_KEY="groq-secret",
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
        cast(Any, Mock()),
    )

    scheduler = build_scheduler(settings(), jobs)

    assert {job.id for job in scheduler.get_jobs()} == {
        "admin_alerts",
        "broadcasts",
        "classification",
        "feedback_cleanup",
        "heartbeat",
        "night_digest",
        "notifications",
        "sync",
    }


@pytest.mark.asyncio
async def test_runtime_job_routes_database_failures_to_direct_alert() -> None:
    foundation = Mock()
    foundation.sync_service.run = AsyncMock(side_effect=SQLAlchemyError("database unavailable"))
    runtime_alerts = Mock()
    runtime_alerts.failed = AsyncMock(return_value=False)
    jobs = RuntimeJobs(
        cast(Foundation, foundation),
        cast(Any, Mock()),
        cast(Any, Mock()),
        cast(Any, runtime_alerts),
    )

    await jobs.sync()

    runtime_alerts.failed.assert_awaited_once_with("postgresql/sync", "SQLAlchemyError")


@pytest.mark.asyncio
async def test_runtime_marks_only_the_first_sync_as_startup() -> None:
    foundation = Mock()
    foundation.sync_service.run = AsyncMock(return_value=object())
    runtime_alerts = Mock()
    jobs = RuntimeJobs(
        cast(Foundation, foundation),
        cast(Any, Mock()),
        cast(Any, Mock()),
        cast(Any, runtime_alerts),
    )

    await jobs.sync()
    await jobs.sync()

    assert foundation.sync_service.run.await_args_list[0].args == (SyncTrigger.STARTUP,)
    assert foundation.sync_service.run.await_args_list[1].args == (SyncTrigger.SCHEDULED,)


def test_docker_runtime_files_are_present() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert (root / "Dockerfile").is_file()
    assert (root / "compose.yaml").is_file()
    assert (root / ".dockerignore").is_file()
    assert "python -m pip uninstall --yes pip" in dockerfile


def test_migration_container_receives_only_database_environment() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))

    migrate = compose["services"]["migrate"]
    app = compose["services"]["app"]
    assert "env_file" not in migrate
    assert set(migrate["environment"]) == {"DATABASE_URL"}
    assert app["env_file"] == ".env"
    assert app["stop_grace_period"] == "360s"
    for service in (migrate, app):
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=64m"]  # noqa: S108


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
            GROQ_API_KEY=secret,
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
