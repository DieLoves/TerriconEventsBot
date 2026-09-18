from dataclasses import replace

import pytest

from terricon_events_bot.domain.catalog import (
    CatalogFilter,
    CatalogPeriod,
    CatalogSection,
    CatalogState,
    EventLanguage,
)
from terricon_events_bot.domain.enums import CategorySlug, EventFormat


def test_catalog_state_round_trip_uses_only_stable_codes() -> None:
    state = CatalogState(
        CatalogFilter(
            section=CatalogSection.ARCHIVE,
            period=CatalogPeriod.MONTHS_3,
            category=CategorySlug.AI_DATA,
            event_format=EventFormat.HYBRID,
            language=EventLanguage.EN,
        ),
        page=3,
    )

    data = state.to_data()

    assert data == {
        "section": "a",
        "period": "3m",
        "category": "ai_data",
        "format": "hybrid",
        "language": "en",
        "page": 3,
    }
    assert CatalogState.from_data(data) == state


def test_catalog_filter_rejects_section_period_mismatch_and_unknown_format() -> None:
    with pytest.raises(ValueError, match="period"):
        CatalogFilter(CatalogSection.UPCOMING, CatalogPeriod.MONTHS_3)
    with pytest.raises(ValueError, match="format"):
        CatalogFilter(
            CatalogSection.ARCHIVE,
            CatalogPeriod.ALL,
            event_format=EventFormat.UNKNOWN,
        )


def test_changing_catalog_filters_resets_page() -> None:
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING), page=4)
    changed = replace(state.filters, language=EventLanguage.RU)

    assert state.with_filters(changed).page == 1
