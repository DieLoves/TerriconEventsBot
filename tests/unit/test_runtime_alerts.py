from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from terricon_events_bot.application.operations import (
    AdminAlertGateway,
    Clock,
    RuntimeFailureAlertService,
)


class FrozenClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class FakeAlertGateway:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_alert(self, text_value: str) -> int:
        self.messages.append(text_value)
        return len(self.messages)


@pytest.mark.asyncio
async def test_runtime_failures_are_grouped_until_recovery() -> None:
    clock = FrozenClock(datetime(2030, 1, 1, 12, tzinfo=UTC))
    gateway = FakeAlertGateway()
    service = RuntimeFailureAlertService(
        cast(AdminAlertGateway, gateway),
        threshold=timedelta(minutes=5),
        clock=cast(Clock, clock),
    )

    assert not await service.failed("postgresql", "OperationalError")
    clock.current += timedelta(minutes=4)
    assert not await service.failed("postgresql", "OperationalError")
    clock.current += timedelta(minutes=1)
    assert await service.failed("postgresql", "OperationalError")
    assert not await service.failed("postgresql", "OperationalError")
    assert len(gateway.messages) == 1
    assert "Компонент: postgresql" in gateway.messages[0]
    assert "Повторений: 3" in gateway.messages[0]

    service.recovered("postgresql")
    assert not await service.failed("postgresql", "OperationalError")
