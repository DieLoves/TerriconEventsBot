from typing import Any, cast

import pytest

from terricon_events_bot.infrastructure.database import (
    SqlAlchemyUnitOfWork,
    create_engine,
    normalize_async_database_url,
)


def test_sync_postgres_url_is_normalized_for_asyncpg() -> None:
    assert (
        normalize_async_database_url("postgresql://user:pass@db/events")
        == "postgresql+asyncpg://user:pass@db/events"
    )


@pytest.mark.asyncio
async def test_engine_can_be_composed_without_connecting() -> None:
    engine = create_engine("postgresql://user:pass@db/events")

    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.sync_engine.hide_parameters is True

    await engine.dispose()


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closes += 1


@pytest.mark.asyncio
async def test_unit_of_work_commits_explicitly_and_closes() -> None:
    session = FakeSession()
    factory = cast(Any, lambda: session)

    async with SqlAlchemyUnitOfWork(factory) as unit_of_work:
        await unit_of_work.commit()

    assert session.commits == 1
    assert session.rollbacks == 0
    assert session.closes == 1


@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_uncommitted_work() -> None:
    session = FakeSession()
    factory = cast(Any, lambda: session)

    async with SqlAlchemyUnitOfWork(factory):
        pass

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.closes == 1
