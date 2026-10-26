from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit

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
# Program records of eventsByStage: the site page carries the application form and
# the card or the date decides between registration and materials.
_DATE_STATUSES = {"unknown_status"}
# Placeholders the source uses instead of empty values.
_STUBS = {"-", "/", "- -"}
_THEME_TAG_PRIORITY = (SourceTheme.IT, SourceTheme.BUSINESS, SourceTheme.MARKETING)
_SITE_HOSTS = {"terricon.kz", "www.terricon.kz"}
_THEME_PAGE = re.compile(r"^/(?:ru|kz)/(?:it|business|marketing)/?$")
_SITE_LOCALE_PREFIX = re.compile(r"^/kz(?=/|$)")
_SITE_FORM_ANCHOR = "form"
_EVENT_TYPE_LIMIT = 64
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_URL_LENGTH = 2048
# RFC 3986 fragment characters; anything else is dropped rather than passed on.
_URL_FRAGMENT = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/?%-]*")


class NormalizationError(ValueError):
    pass


def _is_stub(value: str) -> bool:
    return " ".join(value.split()) in _STUBS


def _clean_optional(value: str) -> str | None:
    normalized = " ".join(value.split())
    if normalized in _STUBS:
        return None
    return normalized or None


def _clean_multiline_optional(value: str) -> str | None:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    cleaned = "\n".join(lines)
    if cleaned in _STUBS:
        return None
    return cleaned or None


def normalize_http_url(value: str, *, keep_fragment: bool = False) -> str | None:
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
    fragment = (
        parsed.fragment
        if keep_fragment and _URL_FRAGMENT.fullmatch(parsed.fragment) is not None
        else ""
    )
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, fragment))


def site_event_url(page_url: str | None, uid: str) -> str | None:
    """Link to one event on the source site: ``<theme page>#<uid>``.

    The site matches ``decodeURIComponent(location.hash)`` against the event ``uid`` and
    opens that event's registration form, so the uid is percent-encoded as is.
    """
    if page_url is None or not uid.strip():
        return page_url
    return urlunsplit(urlsplit(page_url)._replace(fragment=quote(uid.strip(), safe="")))


def site_page_url(page_url: str | None, uid: str) -> str | None:
    """Link to the event on its site page.

    Theme pages open the registration form by the event ``uid``; program pages
    (neconf, hackathon, courses) have a ``#form`` anchor before their form instead.
    """
    if page_url is None:
        return None
    parsed = urlsplit(page_url)
    if (parsed.hostname or "") not in _SITE_HOSTS:
        return page_url
    if _THEME_PAGE.fullmatch(parsed.path) is not None:
        return site_event_url(page_url, uid)
    return urlunsplit(parsed._replace(fragment=_SITE_FORM_ANCHOR))


def canonical_site_url(url: str | None) -> str | None:
    """Locale-neutral form of a site link, so RU and KZ records agree on it."""
    if url is None:
        return None
    parsed = urlsplit(url)
    if (parsed.hostname or "") not in _SITE_HOSTS:
        return url
    return urlunsplit(parsed._replace(path=_SITE_LOCALE_PREFIX.sub("/ru", parsed.path)))


def registration_link(registration_url: str | None, details_url: str | None) -> str | None:
    """Prefer the site registration form over the external short link.

    The form is only offered while the source reports open registration; without an
    event anchor the bare theme page is less useful than the external link.
    """
    external = normalize_http_url(registration_url or "")
    if external is None:
        return None
    site = normalize_http_url(details_url or "", keep_fragment=True)
    if site is not None and urlsplit(site).fragment:
        return site
    return external


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
        if not compact or compact in _STUBS:
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
    try:
        matches_unix = abs(normalized.timestamp() - unix) <= 1
    except (OverflowError, OSError, ValueError):
        matches_unix = False
    if not matches_unix:
        issues.append(NormalizationIssue("date_mismatch", "date_start", "iso_unix"))
    return normalized, issues


def _normalize_url(value: str, field: str, issues: list[NormalizationIssue]) -> str | None:
    if _is_stub(value):
        return None
    normalized = normalize_http_url(value)
    if value.strip() and normalized is None:
        issues.append(NormalizationIssue("invalid_url", field, "invalid"))
    return normalized


def _source_theme(dto: TerriconEventDTO) -> SourceTheme:
    """Theme of an eventsByStage record; ``default`` records fall back to their tags."""
    raw_theme = dto.type.strip().casefold()
    if raw_theme in SourceTheme._value2member_map_:
        return SourceTheme(raw_theme)
    tags = {tag.strip().casefold() for tag in dto.leads_info.tags}
    for theme in _THEME_TAG_PRIORITY:
        if theme.value in tags:
            return theme
    return SourceTheme.IT


def normalize_event(
    dto: TerriconEventDTO,
    locale: Locale,
    expected_theme: SourceTheme | None,
) -> tuple[SourceEvent, tuple[NormalizationIssue, ...]]:
    """Normalize one record; ``expected_theme`` is the theme endpoint, if any."""
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

    if expected_theme is None:
        source_theme = _source_theme(dto)
    else:
        source_theme = expected_theme
        raw_theme = dto.type.strip().casefold()
        if raw_theme != expected_theme.value:
            issues.append(NormalizationIssue("theme_mismatch", "type", raw_theme))

    details_url = site_page_url(_normalize_url(dto.url, "url", issues), dto.uid)
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
    elif status in _DATE_STATUSES:
        registration_url = canonical_site_url(details_url) or action_url
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
            source_theme=source_theme,
            starts_at=starts_at,
            event_format=event_format,
            event_languages=event_languages,
            address=_clean_optional(dto.address),
            registration_url=registration_url,
            recording_url=recording_url,
            poster_url=poster_url,
            localization=localization,
            event_type=dto.event_type.strip().casefold()[:_EVENT_TYPE_LIMIT] or None,
        ),
        tuple(issues),
    )
