from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def normalize_async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        normalize_async_database_url(database_url),
        pool_pre_ping=True,
        hide_parameters=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession | None = None
        self._committed = False

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self.session = self._session_factory()
        self._committed = False
        return self

    async def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("Unit of work has not been entered")
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self.session is None:
            raise RuntimeError("Unit of work has not been entered")
        await self.session.rollback()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is None:
            return
        try:
            if exc_type is not None or not self._committed:
                await self.session.rollback()
        finally:
            await self.session.close()
            self.session = None
