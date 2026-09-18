from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from terricon_events_bot.domain.enums import EventFormat, Locale, SourceTheme


@dataclass(frozen=True, slots=True)
class SourceEventLocalization:
    locale: Locale
    title: str
    description: str | None
    audience: str | None
    speaker: str | None
    details_url: str | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class SourceEvent:
    source_id: int
    source_theme: SourceTheme
    starts_at: datetime
    event_format: EventFormat
    event_languages: tuple[str, ...]
    address: str | None
    registration_url: str | None
    recording_url: str | None
    poster_url: str | None
    localization: SourceEventLocalization


@dataclass(frozen=True, slots=True)
class NormalizationIssue:
    code: str
    field: str
    value: str
