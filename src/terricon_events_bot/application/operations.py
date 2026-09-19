from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.application.sync import SyncResult, SyncService
from terricon_events_bot.domain.enums import SyncTrigger
from terricon_events_bot.infrastructure.models import AdminAlert, AppHeartbeat

T = TypeVar("T")
APP_HEARTBEAT_NAME = "terricon-events-bot"
SCHEMA_REVISION = "0009_stage7_broadcast_state"


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class AdminAlertGateway(Protocol):
    async def send_alert(self, text_value: str) -> int: ...


class SyncRunner(Protocol):
    async def run(self, trigger: SyncTrigger) -> SyncResult: ...


class JobAlreadyRunningError(RuntimeError):
    pass


class SchemaNotReadyError(RuntimeError):
    pass


class AdvisoryJobCoordinator:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def run(
        self,
        name: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T | None:
        key = self._key(name)
        async with self._session_factory() as session:
            acquired = bool(
                await session.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": key},
                )
            )
            if not acquired:
                return None
            try:
                return await operation()
            finally:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": key},
                )

    @staticmethod
    def _key(name: str) -> int:
        digest = hashlib.blake2b(
            f"terricon-events-bot:{name}".encode(),
            digest_size=8,
        ).digest()
        return int.from_bytes(digest, byteorder="big", signed=True)


class LockedSyncRunner:
    def __init__(self, service: SyncService, coordinator: AdvisoryJobCoordinator) -> None:
        self._service = service
        self._coordinator = coordinator

    async def run(self, trigger: SyncTrigger) -> SyncResult:
        result = await self._coordinator.run("sync", lambda: self._service.run(trigger))
        if result is None:
            raise JobAlreadyRunningError("Synchronization is already running")
        return result


@dataclass(frozen=True, slots=True)
class HealthStatus:
    database_ok: bool
    schema_ok: bool
    heartbeat_ok: bool
    heartbeat_age: timedelta | None

    @property
    def healthy(self) -> bool:
        return self.database_ok and self.schema_ok and self.heartbeat_ok


class OperationsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        instance_id: str,
        version: str,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._instance_id = instance_id[:128]
        self._version = version[:64]
        self._clock = clock or SystemClock()

    async def preflight(self) -> None:
        async with self._session_factory() as session:
            await session.execute(text("SELECT 1"))
            try:
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            except Exception as error:
                raise SchemaNotReadyError("Database migrations have not been applied") from error
        if revision != SCHEMA_REVISION:
            raise SchemaNotReadyError(
                f"Database schema is {revision or 'unversioned'}, expected {SCHEMA_REVISION}"
            )

    async def heartbeat(self, *, details: dict[str, object] | None = None) -> None:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(AppHeartbeat)
                    .values(
                        name=APP_HEARTBEAT_NAME,
                        instance_id=self._instance_id,
                        version=self._version,
                        last_seen_at=now,
                        details=details or {},
                    )
                    .on_conflict_do_update(
                        index_elements=[AppHeartbeat.name],
                        set_={
                            "instance_id": self._instance_id,
                            "version": self._version,
                            "last_seen_at": now,
                            "details": details or {},
                        },
                    )
                )

    async def health(self, *, max_heartbeat_age: timedelta) -> HealthStatus:
        if max_heartbeat_age <= timedelta():
            raise ValueError("Heartbeat maximum age must be positive")
        database_ok = False
        schema_ok = False
        heartbeat_age: timedelta | None = None
        try:
            async with self._session_factory() as session:
                database_ok = (await session.scalar(text("SELECT 1"))) == 1
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
                schema_ok = revision == SCHEMA_REVISION
                last_seen_at = await session.scalar(
                    select(AppHeartbeat.last_seen_at).where(AppHeartbeat.name == APP_HEARTBEAT_NAME)
                )
            if last_seen_at is not None:
                heartbeat_age = max(self._now() - last_seen_at, timedelta())
        except Exception:
            return HealthStatus(False, False, False, None)
        return HealthStatus(
            database_ok,
            schema_ok,
            heartbeat_age is not None and heartbeat_age <= max_heartbeat_age,
            heartbeat_age,
        )

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Operations clock must return a timezone-aware datetime")
        return now.astimezone(UTC)


class AdminAlertService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: AdminAlertGateway,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._clock = clock or SystemClock()

    async def dispatch(self, *, limit: int = 20) -> int:
        if limit <= 0:
            raise ValueError("Admin alert limit must be positive")
        async with self._session_factory() as session:
            alert_ids = tuple(
                (
                    await session.scalars(
                        select(AdminAlert.id)
                        .where(
                            AdminAlert.resolved_at.is_(None),
                            (
                                AdminAlert.last_notified_at.is_(None)
                                | (AdminAlert.last_notified_at < AdminAlert.last_seen_at)
                            ),
                        )
                        .order_by(AdminAlert.last_seen_at, AdminAlert.id)
                        .limit(limit)
                    )
                ).all()
            )
        sent = 0
        for alert_id in alert_ids:
            if await self._dispatch_one(alert_id):
                sent += 1
        return sent

    async def _dispatch_one(self, alert_id: int) -> bool:
        async with self._session_factory() as session:
            alert = await session.get(AdminAlert, alert_id)
            if (
                alert is None
                or alert.resolved_at is not None
                or (
                    alert.last_notified_at is not None
                    and alert.last_notified_at >= alert.last_seen_at
                )
            ):
                return False
            observed_at = alert.last_seen_at
            text_value = (
                "Служебное предупреждение\n"
                f"Тип: {alert.kind}\n"
                f"Код: {alert.fingerprint}\n"
                f"Повторений: {alert.occurrence_count}"
            )[:4096]
        await self._gateway.send_alert(text_value)
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                updated = await session.scalar(
                    update(AdminAlert)
                    .where(
                        AdminAlert.id == alert_id,
                        AdminAlert.resolved_at.is_(None),
                        AdminAlert.last_seen_at == observed_at,
                    )
                    .values(last_notified_at=now)
                    .returning(AdminAlert.id)
                )
        return updated is not None

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Admin alert clock must return a timezone-aware datetime")
        return now.astimezone(UTC)
