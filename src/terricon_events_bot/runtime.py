from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiogram import Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from pydantic import ValidationError

from terricon_events_bot.application.operations import (
    AdminAlertService,
    JobAlreadyRunningError,
    OperationsService,
    SchemaNotReadyError,
)
from terricon_events_bot.composition import Foundation, build_foundation
from terricon_events_bot.config import Settings, load_settings
from terricon_events_bot.domain.enums import SyncTrigger
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.logging import configure_logging
from terricon_events_bot.telegram.alerts import AiogramAdminAlertGateway

_LOGGER = logging.getLogger(__name__)
_HEARTBEAT_INTERVAL_SECONDS = 30
_QUEUE_INTERVAL_SECONDS = 30
_HEALTH_MAX_AGE = timedelta(seconds=90)


class RuntimeJobs:
    def __init__(
        self,
        foundation: Foundation,
        operations: OperationsService,
        alerts: AdminAlertService,
    ) -> None:
        self._foundation = foundation
        self._operations = operations
        self._alerts = alerts
        self._active: set[asyncio.Task[Any]] = set()

    async def sync(self) -> None:
        await self._guarded(
            "sync",
            lambda: self._foundation.sync_service.run(SyncTrigger.SCHEDULED),
        )

    async def classification(self) -> None:
        await self._guarded(
            "classification",
            lambda: self._foundation.job_coordinator.run(
                "classification",
                self._foundation.classification_worker.run_batch,
            ),
        )

    async def notifications(self) -> None:
        async def operation() -> tuple[object, object]:
            materialized = await self._foundation.outbox_service.materialize()
            delivered = await self._foundation.delivery_worker.run_batch()
            return materialized, delivered

        await self._guarded("notifications", operation)

    async def broadcasts(self) -> None:
        await self._guarded(
            "broadcasts",
            lambda: self._foundation.job_coordinator.run(
                "broadcasts",
                self._foundation.broadcast_worker.run_batch,
            ),
        )

    async def feedback_cleanup(self) -> None:
        await self._guarded(
            "feedback_cleanup",
            lambda: self._foundation.feedback_service.cleanup(
                self._foundation.settings.feedback_retention_days
            ),
        )

    async def heartbeat(self) -> None:
        await self._guarded(
            "heartbeat",
            lambda: self._operations.heartbeat(
                details={"access_mode": self._foundation.settings.access_mode.value}
            ),
        )

    async def admin_alerts(self) -> None:
        await self._guarded(
            "admin_alerts",
            lambda: self._foundation.job_coordinator.run(
                "admin_alerts",
                self._alerts.dispatch,
            ),
        )

    async def drain(self) -> None:
        active = tuple(task for task in self._active if not task.done())
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def _guarded(
        self,
        name: str,
        operation: Callable[[], Awaitable[object]],
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            result = await operation()
        except JobAlreadyRunningError:
            _LOGGER.info(
                "Scheduled job skipped because its advisory lock is busy", extra={"job": name}
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.exception(
                "Scheduled job failed",
                extra={"error_code": type(error).__name__, "job": name},
            )
        else:
            if result is None and name in {"classification", "broadcasts", "admin_alerts"}:
                _LOGGER.info(
                    "Scheduled job skipped because its advisory lock is busy",
                    extra={"job": name},
                )
            else:
                _LOGGER.debug("Scheduled job completed", extra={"job": name})
        finally:
            if task is not None:
                self._active.discard(task)


def build_scheduler(settings: Settings, jobs: RuntimeJobs) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    common = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 60}
    now = datetime.now(UTC)
    scheduler.add_job(
        jobs.sync,
        "interval",
        hours=settings.sync_interval_hours,
        next_run_time=now,
        id="sync",
        **common,
    )
    scheduler.add_job(
        jobs.classification,
        "interval",
        seconds=_QUEUE_INTERVAL_SECONDS,
        next_run_time=now + timedelta(seconds=5),
        id="classification",
        **common,
    )
    scheduler.add_job(
        jobs.notifications,
        "interval",
        seconds=_QUEUE_INTERVAL_SECONDS,
        next_run_time=now + timedelta(seconds=10),
        id="notifications",
        **common,
    )
    scheduler.add_job(
        jobs.broadcasts,
        "interval",
        seconds=_QUEUE_INTERVAL_SECONDS,
        next_run_time=now + timedelta(seconds=15),
        id="broadcasts",
        **common,
    )
    scheduler.add_job(
        jobs.admin_alerts,
        "interval",
        seconds=60,
        next_run_time=now + timedelta(seconds=20),
        id="admin_alerts",
        **common,
    )
    scheduler.add_job(
        jobs.heartbeat,
        "interval",
        seconds=_HEARTBEAT_INTERVAL_SECONDS,
        next_run_time=now + timedelta(seconds=_HEARTBEAT_INTERVAL_SECONDS),
        id="heartbeat",
        **common,
    )
    scheduler.add_job(
        jobs.feedback_cleanup,
        "cron",
        hour=3,
        minute=30,
        id="feedback_cleanup",
        **common,
    )
    return scheduler


async def run_bot(settings: Settings, project_root: Path) -> None:
    foundation = build_foundation(settings, project_root)
    instance_id = os.environ.get("HOSTNAME") or str(uuid4())
    operations = OperationsService(
        foundation.session_factory,
        instance_id=instance_id,
        version=_package_version(),
    )
    alerts = AdminAlertService(
        foundation.session_factory,
        AiogramAdminAlertGateway(foundation.bot, settings.resolved_admin_chat_id),
    )
    scheduler: AsyncIOScheduler | None = None
    jobs: RuntimeJobs | None = None
    dispatcher = Dispatcher()
    dispatcher.include_router(foundation.telegram_router)
    try:
        await operations.preflight()
        await operations.heartbeat(details={"state": "starting"})
        jobs = RuntimeJobs(foundation, operations, alerts)
        scheduler = build_scheduler(settings, jobs)
        scheduler.start()
        _LOGGER.info("Bot polling started", extra={"access_mode": settings.access_mode.value})
        await dispatcher.start_polling(
            foundation.bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.pause()
        if jobs is not None:
            await jobs.drain()
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        with suppress(Exception):
            await dispatcher.storage.close()
        with suppress(Exception):
            await dispatcher.fsm.events_isolation.close()
        await foundation.close()
        _LOGGER.info("Bot shutdown completed")


async def run_preflight(settings: Settings, project_root: Path) -> None:
    foundation = build_foundation(settings, project_root)
    try:
        operations = OperationsService(
            foundation.session_factory,
            instance_id="preflight",
            version=_package_version(),
        )
        await operations.preflight()
    finally:
        await foundation.close()


async def run_healthcheck(settings: Settings) -> int:
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        operations = OperationsService(
            create_session_factory(engine),
            instance_id="healthcheck",
            version=_package_version(),
        )
        status = await operations.health(max_heartbeat_age=_HEALTH_MAX_AGE)
    finally:
        await engine.dispose()
    print(
        json.dumps(
            {
                "database": status.database_ok,
                "schema": status.schema_ok,
                "heartbeat": status.heartbeat_ok,
            }
        )
    )
    return 0 if status.healthy else 1


def main() -> None:
    settings = _load_runtime_settings()
    _configure_runtime_logging(settings)
    try:
        asyncio.run(run_bot(settings, _project_root()))
    except KeyboardInterrupt:
        return
    except SchemaNotReadyError as error:
        _LOGGER.error("Startup preflight failed", extra={"error_code": str(error)})
        raise SystemExit(2) from error


def preflight_main() -> None:
    settings = _load_runtime_settings()
    _configure_runtime_logging(settings)
    try:
        asyncio.run(run_preflight(settings, _project_root()))
    except SchemaNotReadyError as error:
        _LOGGER.error("Preflight failed", extra={"error_code": str(error)})
        raise SystemExit(2) from error
    print("Preflight OK")


def healthcheck_main() -> None:
    settings = _load_runtime_settings()
    _configure_runtime_logging(settings)
    raise SystemExit(asyncio.run(run_healthcheck(settings)))


def _configure_runtime_logging(settings: Settings) -> None:
    configure_logging(
        settings.log_level,
        (
            settings.telegram_bot_token.get_secret_value(),
            settings.openai_api_key.get_secret_value(),
            settings.database_url.get_secret_value(),
        ),
    )


def _load_runtime_settings() -> Settings:
    try:
        return load_settings()
    except ValidationError as error:
        fields = sorted(
            {
                ".".join(str(part) for part in item["loc"])
                for item in error.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            }
        )
        summary = ", ".join(fields) if fields else "unknown"
        print(f"Invalid configuration fields: {summary}", file=sys.stderr)
        raise SystemExit(2) from None


def _project_root() -> Path:
    configured = os.environ.get("PROJECT_ROOT")
    root = Path(configured) if configured else Path.cwd()
    if not (root / "locales").is_dir():
        raise RuntimeError("PROJECT_ROOT must contain locales/")
    return root


def _package_version() -> str:
    try:
        return version("terricon-events-bot")
    except PackageNotFoundError:
        return "0.1.0-dev"
