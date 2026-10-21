from __future__ import annotations

# ruff: noqa: RUF001 - Russian admin alerts intentionally mix technical terms.
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.application.sync import SyncResult, SyncService
from terricon_events_bot.domain.enums import ClassificationStatus, DeliveryStatus, SyncTrigger
from terricon_events_bot.infrastructure.models import (
    AdminAlert,
    AppHeartbeat,
    BroadcastDelivery,
    EventClassification,
    NotificationOutbox,
)

T = TypeVar("T")
APP_HEARTBEAT_NAME = "terricon-events-bot"
SCHEMA_REVISION = "0010_hardening_queue_state"
_PROLONGED_QUEUE_AGE = timedelta(hours=1)
_OPERATIONAL_ALERT_KINDS = frozenset(
    {
        "gpt_prolonged_failure",
        "outbox_prolonged_failure",
        "broadcast_prolonged_failure",
    }
)


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


@dataclass(slots=True)
class _RuntimeFailure:
    first_seen_at: datetime
    occurrences: int = 1
    notified: bool = False


class RuntimeFailureAlertService:
    """Alerts without PostgreSQL when the database itself is unavailable."""

    def __init__(
        self,
        gateway: AdminAlertGateway,
        *,
        threshold: timedelta = timedelta(minutes=5),
        clock: Clock | None = None,
    ) -> None:
        if threshold <= timedelta():
            raise ValueError("Runtime alert threshold must be positive")
        self._gateway = gateway
        self._threshold = threshold
        self._clock = clock or SystemClock()
        self._failures: dict[tuple[str, str], _RuntimeFailure] = {}

    async def failed(self, component: str, error_code: str) -> bool:
        now = self._now()
        key = (component[:64], error_code[:128])
        failure = self._failures.get(key)
        if failure is None:
            self._failures[key] = _RuntimeFailure(now)
            return False
        failure.occurrences += 1
        if failure.notified or now - failure.first_seen_at < self._threshold:
            return False
        await self._gateway.send_alert(
            (
                "Служебное предупреждение\n"
                f"Компонент: {key[0]}\n"
                f"Код: {key[1]}\n"
                f"Повторений: {failure.occurrences}"
            )[:4096]
        )
        failure.notified = True
        return True

    def recovered(self, component: str) -> None:
        normalized = component[:64]
        self._failures = {
            key: failure for key, failure in self._failures.items() if key[0] != normalized
        }

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Runtime alert clock must return a timezone-aware datetime")
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

    async def refresh_operational(self) -> int:
        now = self._now()
        async with self._session_factory() as session:
            async with session.begin():
                groups = await self._operational_groups(session, now)
                existing = {
                    alert.fingerprint: alert
                    for alert in (
                        await session.scalars(
                            select(AdminAlert).where(
                                AdminAlert.kind.in_(_OPERATIONAL_ALERT_KINDS),
                                AdminAlert.resolved_at.is_(None),
                            )
                        )
                    ).all()
                }
                for fingerprint, (kind, details, affected) in groups.items():
                    alert = existing.get(fingerprint)
                    if alert is None:
                        session.add(
                            AdminAlert(
                                kind=kind,
                                fingerprint=fingerprint,
                                details=details,
                                occurrence_count=affected,
                                first_seen_at=now,
                                last_seen_at=now,
                            )
                        )
                    elif alert.details != details:
                        alert.details = details
                        alert.occurrence_count += 1
                        alert.last_seen_at = now
                resolved = set(existing) - set(groups)
                if resolved:
                    await session.execute(
                        update(AdminAlert)
                        .where(
                            AdminAlert.fingerprint.in_(resolved),
                            AdminAlert.resolved_at.is_(None),
                        )
                        .values(resolved_at=now)
                    )
        return len(groups)

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
            error_code = alert.details.get("error_code")
            affected = alert.details.get("affected")
            if isinstance(error_code, str):
                text_value = f"{text_value}\nПричина: {error_code}"[:4096]
            if isinstance(affected, int) and not isinstance(affected, bool):
                text_value = f"{text_value}\nЗатронуто: {affected}"[:4096]
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

    @staticmethod
    async def _operational_groups(
        session: AsyncSession,
        now: datetime,
    ) -> dict[str, tuple[str, dict[str, object], int]]:
        cutoff = now - _PROLONGED_QUEUE_AGE
        queries = (
            (
                "gpt_prolonged_failure",
                select(EventClassification.last_error_code, func.count())
                .where(
                    EventClassification.status == ClassificationStatus.FALLBACK,
                    EventClassification.last_error_code.is_not(None),
                )
                .group_by(EventClassification.last_error_code),
            ),
            (
                "outbox_prolonged_failure",
                select(NotificationOutbox.last_error_code, func.count())
                .where(
                    NotificationOutbox.last_error_code.is_not(None),
                    or_(
                        NotificationOutbox.status == DeliveryStatus.FAILED,
                        and_(
                            NotificationOutbox.status == DeliveryStatus.RETRY_WAIT,
                            NotificationOutbox.created_at <= cutoff,
                        ),
                    ),
                )
                .group_by(NotificationOutbox.last_error_code),
            ),
            (
                "broadcast_prolonged_failure",
                select(BroadcastDelivery.last_error_code, func.count())
                .where(
                    BroadcastDelivery.last_error_code.is_not(None),
                    or_(
                        BroadcastDelivery.status == DeliveryStatus.FAILED,
                        and_(
                            BroadcastDelivery.status == DeliveryStatus.RETRY_WAIT,
                            BroadcastDelivery.created_at <= cutoff,
                        ),
                    ),
                )
                .group_by(BroadcastDelivery.last_error_code),
            ),
        )
        groups: dict[str, tuple[str, dict[str, object], int]] = {}
        for kind, query in queries:
            rows = (await session.execute(query)).all()
            for raw_error_code, raw_affected in rows:
                if not isinstance(raw_error_code, str):
                    continue
                affected = int(raw_affected)
                digest = hashlib.sha256(raw_error_code.encode()).hexdigest()[:20]
                fingerprint = f"{kind}:{digest}"
                groups[fingerprint] = (
                    kind,
                    {"error_code": raw_error_code, "affected": affected},
                    affected,
                )
        return groups

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Admin alert clock must return a timezone-aware datetime")
        return now.astimezone(UTC)
