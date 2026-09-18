from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from terricon_events_bot.domain.catalog import (
    CatalogFilter,
    CatalogPage,
    CatalogPeriod,
    CatalogSection,
    CatalogState,
    EventDetails,
    EventSummary,
)
from terricon_events_bot.domain.enums import CategorySlug, EventState, Locale
from terricon_events_bot.infrastructure.models import (
    Event,
    EventCategory,
    EventLocalization,
    FsmState,
    SyncState,
)


class CatalogQuery:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        timezone: ZoneInfo,
        page_size: int,
    ) -> None:
        if page_size <= 0:
            raise ValueError("Catalog page size must be positive")
        self._session_factory = session_factory
        self._timezone = timezone
        self._page_size = page_size

    async def list(
        self,
        filters: CatalogFilter,
        locale: Locale,
        page: int,
        *,
        now: datetime | None = None,
    ) -> CatalogPage:
        if page < 1:
            raise ValueError("Catalog page must be positive")
        current = self._normalize_now(now or datetime.now(UTC))
        conditions = self._conditions(filters, current)
        preferred = aliased(EventLocalization, name="preferred_localization")
        fallback = aliased(EventLocalization, name="fallback_localization")
        fallback_locale = Locale.KZ if locale is Locale.RU else Locale.RU

        async with self._session_factory() as session:
            total = int(
                await session.scalar(select(func.count()).select_from(Event).where(*conditions))
                or 0
            )
            pages = max(1, (total + self._page_size - 1) // self._page_size)
            selected_page = min(page, pages)
            order = (
                (Event.starts_at.asc(), Event.id.asc())
                if filters.section is CatalogSection.UPCOMING
                else (Event.starts_at.desc(), Event.id.desc())
            )
            rows = (
                await session.execute(
                    select(Event, preferred, fallback)
                    .outerjoin(
                        preferred,
                        (preferred.event_id == Event.id) & (preferred.locale == locale),
                    )
                    .outerjoin(
                        fallback,
                        (fallback.event_id == Event.id) & (fallback.locale == fallback_locale),
                    )
                    .where(
                        *conditions,
                        preferred.event_id.is_not(None) | fallback.event_id.is_not(None),
                    )
                    .order_by(*order)
                    .offset((selected_page - 1) * self._page_size)
                    .limit(self._page_size)
                )
            ).all()
            event_ids = [event.id for event, _, _ in rows]
            categories = await self._categories(session, event_ids)
            sync_state = await session.get(SyncState, 1)

        items = tuple(
            EventSummary(
                id=event.id,
                title=(preferred_row or fallback_row).title,
                starts_at=event.starts_at,
                event_format=event.event_format,
                categories=categories[event.id],
                translation_source=(preferred_row or fallback_row).translation_source,
            )
            for event, preferred_row, fallback_row in rows
        )
        stale = bool(
            sync_state is None
            or sync_state.last_full_success_at is None
            or (
                sync_state.last_attempt_at is not None
                and sync_state.last_attempt_at > sync_state.last_full_success_at
            )
        )
        return CatalogPage(
            items=items,
            page=selected_page,
            pages=pages,
            total=total,
            stale=stale,
        )

    async def get(self, event_id: int, locale: Locale) -> EventDetails | None:
        if event_id <= 0:
            return None
        preferred = aliased(EventLocalization, name="preferred_localization")
        fallback = aliased(EventLocalization, name="fallback_localization")
        fallback_locale = Locale.KZ if locale is Locale.RU else Locale.RU
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Event, preferred, fallback)
                    .outerjoin(
                        preferred,
                        (preferred.event_id == Event.id) & (preferred.locale == locale),
                    )
                    .outerjoin(
                        fallback,
                        (fallback.event_id == Event.id) & (fallback.locale == fallback_locale),
                    )
                    .where(
                        Event.id == event_id,
                        preferred.event_id.is_not(None) | fallback.event_id.is_not(None),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            event, preferred_row, fallback_row = row
            localization = preferred_row or fallback_row
            categories = await self._categories(session, [event.id])
        return EventDetails(
            id=event.id,
            title=localization.title,
            description=localization.description,
            audience=localization.audience,
            speaker=localization.speaker,
            details_url=localization.details_url,
            starts_at=event.starts_at,
            event_format=event.event_format,
            event_languages=tuple(event.event_languages),
            address=event.address,
            registration_url=event.registration_url,
            recording_url=event.recording_url,
            poster_url=event.poster_url,
            is_available=event.is_available,
            categories=categories[event.id],
            locale=localization.locale,
            translation_source=localization.translation_source,
        )

    async def adjacent_ids(
        self,
        filters: CatalogFilter,
        event_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[int | None, int | None] | None:
        if event_id <= 0:
            return None
        current = self._normalize_now(now or datetime.now(UTC))
        order = (
            (Event.starts_at.asc(), Event.id.asc())
            if filters.section is CatalogSection.UPCOMING
            else (Event.starts_at.desc(), Event.id.desc())
        )
        ordered = (
            select(
                Event.id.label("event_id"),
                func.lag(Event.id).over(order_by=order).label("previous_id"),
                func.lead(Event.id).over(order_by=order).label("following_id"),
            )
            .where(*self._conditions(filters, current))
            .subquery()
        )
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ordered.c.previous_id, ordered.c.following_id).where(
                        ordered.c.event_id == event_id
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        previous_id, following_id = row
        return (
            int(previous_id) if previous_id is not None else None,
            int(following_id) if following_id is not None else None,
        )

    def _conditions(self, filters: CatalogFilter, now: datetime) -> tuple[ColumnElement[bool], ...]:
        today = now.astimezone(self._timezone).date()
        today_start = self._day_start(today)
        conditions: list[ColumnElement[bool]] = []
        if filters.section is CatalogSection.UPCOMING:
            conditions.extend(
                (
                    Event.starts_at >= today_start,
                    Event.is_available.is_(True),
                    Event.state == EventState.ACTIVE,
                )
            )
            if filters.period is CatalogPeriod.TODAY:
                conditions.append(Event.starts_at < self._day_start(today + timedelta(days=1)))
            elif filters.period is CatalogPeriod.DAYS_7:
                conditions.append(Event.starts_at < self._day_start(today + timedelta(days=7)))
            elif filters.period is CatalogPeriod.DAYS_30:
                conditions.append(Event.starts_at < self._day_start(today + timedelta(days=30)))
        else:
            conditions.append(Event.starts_at < today_start)
            if filters.period is CatalogPeriod.DAYS_30:
                conditions.append(Event.starts_at >= self._day_start(today - timedelta(days=30)))
            elif filters.period is CatalogPeriod.MONTHS_3:
                conditions.append(
                    Event.starts_at >= self._day_start(self._subtract_months(today, 3))
                )

        if filters.category is not None:
            conditions.append(
                exists(
                    select(EventCategory.event_id).where(
                        EventCategory.event_id == Event.id,
                        EventCategory.category == filters.category,
                    )
                )
            )
        if filters.event_format is not None:
            conditions.append(Event.event_format == filters.event_format)
        if filters.language is not None:
            conditions.append(Event.event_languages.contains([filters.language.value]))
        conditions.append(
            exists(select(EventLocalization.event_id).where(EventLocalization.event_id == Event.id))
        )
        return tuple(conditions)

    async def _categories(
        self, session: AsyncSession, event_ids: Sequence[int]
    ) -> dict[int, tuple[CategorySlug, ...]]:
        grouped: dict[int, list[CategorySlug]] = defaultdict(list)
        if event_ids:
            rows = (
                await session.execute(
                    select(EventCategory.event_id, EventCategory.category)
                    .where(EventCategory.event_id.in_(event_ids))
                    .order_by(EventCategory.event_id, EventCategory.category)
                )
            ).all()
            for event_id, category in rows:
                grouped[event_id].append(category)
        return {event_id: tuple(grouped[event_id]) for event_id in event_ids}

    def _day_start(self, value: date) -> datetime:
        return datetime.combine(value, time.min, self._timezone).astimezone(UTC)

    @staticmethod
    def _subtract_months(value: date, months: int) -> date:
        month_index = value.year * 12 + value.month - 1 - months
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        return value.replace(
            year=year,
            month=month,
            day=min(value.day, calendar.monthrange(year, month)[1]),
        )

    @staticmethod
    def _normalize_now(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Catalog clock must be timezone-aware")
        return value.astimezone(UTC)


class CatalogStateStore:
    _SCOPE = "catalog"
    _STATE = "filters"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load(
        self, user_id: int, default_section: CatalogSection = CatalogSection.UPCOMING
    ) -> CatalogState:
        async with self._session_factory() as session:
            row = await session.get(FsmState, (user_id, self._SCOPE))
        if row is None:
            return CatalogState(CatalogFilter.default(default_section))
        try:
            return CatalogState.from_data(row.data)
        except (KeyError, TypeError, ValueError):
            return CatalogState(CatalogFilter.default(default_section))

    async def save(self, user_id: int, state: CatalogState) -> None:
        statement = pg_insert(FsmState).values(
            user_id=user_id,
            scope=self._SCOPE,
            state=self._STATE,
            data=state.to_data(),
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[FsmState.user_id, FsmState.scope],
                        set_={"state": self._STATE, "data": state.to_data()},
                    )
                )
