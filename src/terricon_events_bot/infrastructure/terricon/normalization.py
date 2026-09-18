from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from terricon_events_bot.domain.enums import EventFormat, Locale, SourceTheme
from terricon_events_bot.domain.events import (
    NormalizationIssue,
    SourceEvent,
    SourceEventLocalization,
)
from terricon_events_bot.infrastructure.terricon.dto import TerriconEventDTO

_FORMAT_MAP = {
    "online": EventFormat.ONLINE,
    "offline": EventFormat.OFFLINE,
    "online_and_offline": EventFormat.HYBRID,
}
_LANGUAGE_MAP = {
    "english": "en",
    "английский": "en",
    "қазақша": "kz",
    "казахский": "kz",
    "русский": "ru",
    "russian": "ru",
}
_ACTIVE_STATUSES = {"new_mitap"}
_ARCHIVE_STATUSES = {"end_mitap"}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_URL_LENGTH = 2048


class NormalizationError(ValueError):
    pass


def _clean_optional(value: str) -> str | None:
    normalized = " ".join(value.split())
    return normalized or None


def _clean_multiline_optional(value: str) -> str | None:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) or None


def normalize_http_url(value: str) -> str | None:
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > _MAX_URL_LENGTH
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in candidate
        )
        or "\\" in candidate
    ):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        return None
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return None
    if not hostname or len(hostname) > 253 or "%" in hostname:
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(labels) < 2 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            return None
    else:
        if not address.is_global:
            return None
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _content_hash(localization: dict[str, str | None]) -> str:
    payload = json.dumps(
        localization,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _normalize_languages(values: list[str]) -> tuple[tuple[str, ...], list[NormalizationIssue]]:
    normalized: set[str] = set()
    issues: list[NormalizationIssue] = []
    for value in values:
        compact = unicodedata.normalize("NFKC", value).strip().casefold()
        if not compact:
            continue
        language = _LANGUAGE_MAP.get(compact)
        if language is None:
            language = compact
            issues.append(NormalizationIssue("unknown_language", "language", compact))
        normalized.add(language)
    return tuple(sorted(normalized)), issues


def _normalize_date(value: datetime, unix: int) -> tuple[datetime, list[NormalizationIssue]]:
    issues: list[NormalizationIssue] = []
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
        issues.append(NormalizationIssue("naive_datetime", "date_start.iso", "naive"))
    normalized = value.astimezone(UTC)
    if abs(normalized.timestamp() - unix) > 1:
        issues.append(NormalizationIssue("date_mismatch", "date_start", "iso_unix"))
    return normalized, issues


def _normalize_url(value: str, field: str, issues: list[NormalizationIssue]) -> str | None:
    normalized = normalize_http_url(value)
    if value.strip() and normalized is None:
        issues.append(NormalizationIssue("invalid_url", field, "invalid"))
    return normalized


def normalize_event(
    dto: TerriconEventDTO,
    locale: Locale,
    expected_theme: SourceTheme,
) -> tuple[SourceEvent, tuple[NormalizationIssue, ...]]:
    issues: list[NormalizationIssue] = []
    starts_at, date_issues = _normalize_date(
        dto.card_info.date_start.iso, dto.card_info.date_start.unix
    )
    issues.extend(date_issues)

    raw_format = dto.leads_info.format.id.strip().casefold()
    event_format = _FORMAT_MAP.get(raw_format, EventFormat.UNKNOWN)
    if event_format is EventFormat.UNKNOWN:
        issues.append(NormalizationIssue("unknown_format", "format", raw_format))

    event_languages, language_issues = _normalize_languages(dto.card_info.language)
    issues.extend(language_issues)

    raw_theme = dto.type.strip().casefold()
    if raw_theme != expected_theme.value:
        issues.append(NormalizationIssue("theme_mismatch", "type", raw_theme))

    details_url = _normalize_url(dto.url, "url", issues)
    complete_url = _normalize_url(dto.card_info.complete_url, "complete_url", issues)
    live_url = _normalize_url(dto.card_info.url_live, "url_live", issues)
    if complete_url and live_url and complete_url != live_url:
        issues.append(NormalizationIssue("source_url_mismatch", "complete_url", "url_live"))
    action_url = live_url or complete_url

    status = dto.leads_info.status.strip().casefold()
    registration_url: str | None = None
    recording_url: str | None = None
    if status in _ACTIVE_STATUSES:
        registration_url = action_url
    elif status in _ARCHIVE_STATUSES:
        recording_url = action_url
    else:
        registration_url = action_url
        recording_url = action_url
        issues.append(NormalizationIssue("unknown_status", "status", status))

    title = _clean_optional(dto.card_info.name)
    if title is None:
        raise NormalizationError("Event title is empty")
    speaker = _clean_optional(dto.contact.full_name)
    if speaker is None:
        speaker = _clean_optional(f"{dto.contact.first_name} {dto.contact.last_name}")

    localization_data = {
        "audience": _clean_optional(dto.card_info.to_whom),
        "description": _clean_multiline_optional(dto.card_info.description),
        "details_url": details_url,
        "speaker": speaker,
        "title": title,
    }
    localization = SourceEventLocalization(
        locale=locale,
        title=title,
        description=localization_data["description"],
        audience=localization_data["audience"],
        speaker=speaker,
        details_url=details_url,
        content_hash=_content_hash(localization_data),
    )
    poster_url = _normalize_url(dto.leads_info.url_photo, "url_photo", issues)

    return (
        SourceEvent(
            source_id=dto.id,
            source_theme=expected_theme,
            starts_at=starts_at,
            event_format=event_format,
            event_languages=event_languages,
            address=_clean_optional(dto.address),
            registration_url=registration_url,
            recording_url=recording_url,
            poster_url=poster_url,
            localization=localization,
        ),
        tuple(issues),
    )
