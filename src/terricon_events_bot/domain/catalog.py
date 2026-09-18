from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from terricon_events_bot.domain.enums import (
    CategorySlug,
    EventFormat,
    Locale,
    TranslationSource,
)


class CatalogSection(StrEnum):
    UPCOMING = "u"
    ARCHIVE = "a"


class CatalogPeriod(StrEnum):
    TODAY = "t"
    DAYS_7 = "7"
    DAYS_30 = "30"
    MONTHS_3 = "3m"
    ALL = "a"


class EventLanguage(StrEnum):
    RU = "ru"
    KZ = "kz"
    EN = "en"


_SECTION_PERIODS = {
    CatalogSection.UPCOMING: frozenset(
        (
            CatalogPeriod.TODAY,
            CatalogPeriod.DAYS_7,
            CatalogPeriod.DAYS_30,
            CatalogPeriod.ALL,
        )
    ),
    CatalogSection.ARCHIVE: frozenset(
        (
            CatalogPeriod.DAYS_30,
            CatalogPeriod.MONTHS_3,
            CatalogPeriod.ALL,
        )
    ),
}


@dataclass(frozen=True, slots=True)
class CatalogFilter:
    section: CatalogSection
    period: CatalogPeriod
    category: CategorySlug | None = None
    event_format: EventFormat | None = None
    language: EventLanguage | None = None

    def __post_init__(self) -> None:
        if self.period not in _SECTION_PERIODS[self.section]:
            raise ValueError("Catalog period does not belong to the selected section")
        if self.event_format is EventFormat.UNKNOWN:
            raise ValueError("Unknown is not a selectable catalog format")

    @classmethod
    def default(cls, section: CatalogSection) -> CatalogFilter:
        return cls(section=section, period=CatalogPeriod.ALL)


@dataclass(frozen=True, slots=True)
class CatalogState:
    filters: CatalogFilter
    page: int = 1

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("Catalog page must be positive")

    def with_filters(self, filters: CatalogFilter) -> CatalogState:
        return replace(self, filters=filters, page=1)

    def to_data(self) -> dict[str, object]:
        return {
            "section": self.filters.section.value,
            "period": self.filters.period.value,
            "category": (
                self.filters.category.value if self.filters.category is not None else None
            ),
            "format": (
                self.filters.event_format.value if self.filters.event_format is not None else None
            ),
            "language": (
                self.filters.language.value if self.filters.language is not None else None
            ),
            "page": self.page,
        }

    @classmethod
    def from_data(cls, data: object) -> CatalogState:
        if not isinstance(data, dict):
            raise ValueError("Catalog state must be an object")
        section = CatalogSection(data["section"])
        category_value = data.get("category")
        format_value = data.get("format")
        language_value = data.get("language")
        page = data.get("page", 1)
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError("Catalog page must be positive")
        return cls(
            filters=CatalogFilter(
                section=section,
                period=CatalogPeriod(data["period"]),
                category=CategorySlug(category_value) if category_value is not None else None,
                event_format=EventFormat(format_value) if format_value is not None else None,
                language=EventLanguage(language_value) if language_value is not None else None,
            ),
            page=page,
        )


@dataclass(frozen=True, slots=True)
class EventSummary:
    id: int
    title: str
    starts_at: datetime
    event_format: EventFormat
    categories: tuple[CategorySlug, ...]
    translation_source: TranslationSource


@dataclass(frozen=True, slots=True)
class EventDetails:
    id: int
    title: str
    description: str | None
    audience: str | None
    speaker: str | None
    details_url: str | None
    starts_at: datetime
    event_format: EventFormat
    event_languages: tuple[str, ...]
    address: str | None
    registration_url: str | None
    recording_url: str | None
    poster_url: str | None
    is_available: bool
    categories: tuple[CategorySlug, ...]
    locale: Locale
    translation_source: TranslationSource


@dataclass(frozen=True, slots=True)
class CatalogPage:
    items: tuple[EventSummary, ...]
    page: int
    pages: int
    total: int
    stale: bool
