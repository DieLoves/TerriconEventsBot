from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.domain.enums import CategorySlug
from terricon_events_bot.infrastructure.models import (
    SubscriptionCategory,
    SubscriptionSettings,
    User,
)

_CATEGORY_ORDER = {category: index for index, category in enumerate(CategorySlug)}


class SubscriptionMode(StrEnum):
    NONE = "none"
    ALL = "all"
    CATEGORIES = "categories"


@dataclass(frozen=True, slots=True)
class SubscriptionSummary:
    subscribe_all: bool
    categories: tuple[CategorySlug, ...]
    # Accelerators, incubators and courses; independent of the event subscription.
    programs: bool = False


class SubscriptionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: int) -> SubscriptionSummary:
        async with self._session_factory() as session:
            return await self._get(session, user_id)

    async def replace(
        self,
        user_id: int,
        mode: SubscriptionMode,
        categories: tuple[CategorySlug, ...] = (),
    ) -> SubscriptionSummary:
        normalized = self._validate(mode, categories)
        async with self._session_factory() as session:
            async with session.begin():
                programs = await self._lock_user(session, user_id)
                await self._replace(session, user_id, mode, normalized)
                return SubscriptionSummary(
                    subscribe_all=mode is SubscriptionMode.ALL,
                    categories=normalized,
                    programs=programs,
                )

    async def toggle_all(self, user_id: int) -> SubscriptionSummary:
        async with self._session_factory() as session:
            async with session.begin():
                await self._lock_user(session, user_id)
                current = await self._get(session, user_id)
                mode = SubscriptionMode.NONE if current.subscribe_all else SubscriptionMode.ALL
                await self._replace(session, user_id, mode, ())
                return SubscriptionSummary(
                    subscribe_all=mode is SubscriptionMode.ALL,
                    categories=(),
                    programs=current.programs,
                )

    async def toggle_category(self, user_id: int, category: CategorySlug) -> SubscriptionSummary:
        async with self._session_factory() as session:
            async with session.begin():
                await self._lock_user(session, user_id)
                current = await self._get(session, user_id)
                categories = set(current.categories)
                if current.subscribe_all:
                    categories = {category}
                elif category in categories:
                    categories.remove(category)
                else:
                    categories.add(category)
                normalized = tuple(sorted(categories, key=_CATEGORY_ORDER.__getitem__))
                mode = SubscriptionMode.CATEGORIES if normalized else SubscriptionMode.NONE
                await self._replace(session, user_id, mode, normalized)
                return SubscriptionSummary(
                    subscribe_all=False, categories=normalized, programs=current.programs
                )

    async def toggle_programs(self, user_id: int) -> SubscriptionSummary:
        async with self._session_factory() as session:
            async with session.begin():
                programs = not await self._lock_user(session, user_id)
                await session.execute(
                    update(User).where(User.id == user_id).values(program_notifications=programs)
                )
                current = await self._get(session, user_id)
                return SubscriptionSummary(current.subscribe_all, current.categories, programs)

    @staticmethod
    async def _lock_user(session: AsyncSession, user_id: int) -> bool:
        """Lock the user row; returns the program subscription flag."""
        programs = await session.scalar(
            select(User.program_notifications).where(User.id == user_id).with_for_update()
        )
        if programs is None:
            raise LookupError("User does not exist")
        return programs

    @staticmethod
    async def _get(session: AsyncSession, user_id: int) -> SubscriptionSummary:
        settings = await session.get(SubscriptionSettings, user_id)
        categories = tuple(
            (
                await session.scalars(
                    select(SubscriptionCategory.category)
                    .where(SubscriptionCategory.user_id == user_id)
                    .order_by(SubscriptionCategory.category)
                )
            ).all()
        )
        programs = await session.scalar(
            select(User.program_notifications).where(User.id == user_id)
        )
        return SubscriptionSummary(
            subscribe_all=bool(settings and settings.subscribe_all),
            categories=categories,
            programs=bool(programs),
        )

    @staticmethod
    async def _replace(
        session: AsyncSession,
        user_id: int,
        mode: SubscriptionMode,
        categories: tuple[CategorySlug, ...],
    ) -> None:
        await session.execute(
            delete(SubscriptionCategory).where(SubscriptionCategory.user_id == user_id)
        )
        settings = await session.get(SubscriptionSettings, user_id)
        if mode is SubscriptionMode.NONE:
            if settings is not None:
                await session.delete(settings)
            return
        if settings is None:
            settings = SubscriptionSettings(user_id=user_id)
            session.add(settings)
        settings.subscribe_all = mode is SubscriptionMode.ALL
        await session.flush()
        if mode is SubscriptionMode.CATEGORIES:
            session.add_all(
                SubscriptionCategory(user_id=user_id, category=category) for category in categories
            )

    @staticmethod
    def _validate(
        mode: SubscriptionMode,
        categories: tuple[CategorySlug, ...],
    ) -> tuple[CategorySlug, ...]:
        normalized = tuple(sorted(set(categories), key=_CATEGORY_ORDER.__getitem__))
        if mode is SubscriptionMode.CATEGORIES and not normalized:
            raise ValueError("Category mode requires at least one category")
        if mode is not SubscriptionMode.CATEGORIES and normalized:
            raise ValueError("Categories are only allowed in category mode")
        return normalized
