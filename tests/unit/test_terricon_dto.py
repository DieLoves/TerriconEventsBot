from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from terricon_events_bot.domain.enums import EventFormat, Locale, SourceTheme
from terricon_events_bot.infrastructure.terricon.dto import TerriconEventDTO
from terricon_events_bot.infrastructure.terricon.normalization import (
    NormalizationError,
    normalize_event,
    normalize_http_url,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "terricon"


def load_fixture(locale: Locale, theme: SourceTheme) -> dict[str, Any]:
    path = FIXTURES / f"{locale.value}_{theme.value}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == 1
    assert isinstance(raw[0], dict)
    return raw[0]


@pytest.mark.parametrize("locale", list(Locale))
@pytest.mark.parametrize("theme", list(SourceTheme))
def test_all_six_synthetic_fixtures_match_actual_contract(
    locale: Locale, theme: SourceTheme
) -> None:
    dto = TerriconEventDTO.model_validate(load_fixture(locale, theme))

    event, issues = normalize_event(dto, locale, theme)

    assert event.source_theme is theme
    assert event.localization.locale is locale
    assert event.localization.title
    assert event.localization.content_hash
    assert event.starts_at.tzinfo is not None
    assert not issues


def test_normalization_maps_format_languages_urls_and_archive_link() -> None:
    dto = TerriconEventDTO.model_validate(load_fixture(Locale.RU, SourceTheme.BUSINESS))

    event, issues = normalize_event(dto, Locale.RU, SourceTheme.BUSINESS)

    assert not issues
    assert event.event_format is EventFormat.OFFLINE
    assert event.event_languages == ("ru",)
    assert event.registration_url is None
    assert event.recording_url == "https://recordings.example/business"
    assert event.localization.details_url == "https://events.example/ru/business/fixture-business"


def test_unknown_values_are_preserved_safely_and_reported() -> None:
    raw = deepcopy(load_fixture(Locale.RU, SourceTheme.IT))
    raw["type"] = "unexpected-theme"
    raw["card_info"]["language"] = ["Español", "русский"]
    raw["leads_info"]["format"]["id"] = "teleport"
    raw["leads_info"]["status"] = "unexpected-status"
    raw["leads_info"]["url_photo"] = "javascript:alert(1)"
    dto = TerriconEventDTO.model_validate(raw)

    event, issues = normalize_event(dto, Locale.RU, SourceTheme.IT)

    assert event.source_theme is SourceTheme.IT
    assert event.event_format is EventFormat.UNKNOWN
    assert event.event_languages == ("español", "ru")
    assert event.poster_url is None
    assert event.registration_url == "https://registration.example/it"
    assert event.recording_url == "https://registration.example/it"
    assert {issue.code for issue in issues} == {
        "invalid_url",
        "theme_mismatch",
        "unknown_format",
        "unknown_language",
        "unknown_status",
    }


def test_empty_title_rejects_only_the_invalid_record() -> None:
    raw = deepcopy(load_fixture(Locale.RU, SourceTheme.IT))
    raw["card_info"]["name"] = "  "
    dto = TerriconEventDTO.model_validate(raw)

    with pytest.raises(NormalizationError, match="title is empty"):
        normalize_event(dto, Locale.RU, SourceTheme.IT)


def test_description_preserves_line_breaks_while_cleaning_horizontal_space() -> None:
    raw = deepcopy(load_fixture(Locale.RU, SourceTheme.IT))
    raw["card_info"]["description"] = "  First   line\r\nSecond\tline\n\nThird line  "
    dto = TerriconEventDTO.model_validate(raw)

    event, _ = normalize_event(dto, Locale.RU, SourceTheme.IT)

    assert event.localization.description == "First line\nSecond line\n\nThird line"


def test_source_id_is_strictly_positive_integer() -> None:
    raw = deepcopy(load_fixture(Locale.RU, SourceTheme.IT))
    raw["id"] = "101"

    with pytest.raises(ValidationError):
        TerriconEventDTO.model_validate(raw)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" HTTPS://Example.COM/path#fragment ", "https://example.com/path"),
        ("ftp://example.com/file", None),
        ("relative/path", None),
        ("https://user:password@example.com/path", None),
        ("https://127.0.0.1/internal", None),
        ("https://example.com/path with space", None),
        ("https://localhost/path", None),
        ("", None),
    ],
)
def test_http_url_normalization(value: str, expected: str | None) -> None:
    assert normalize_http_url(value) == expected
