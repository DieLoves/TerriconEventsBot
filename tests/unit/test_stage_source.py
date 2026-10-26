from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from terricon_events_bot.domain.enums import EventFormat, Locale, SourceTheme
from terricon_events_bot.domain.event_kinds import (
    EventKind,
    event_kind,
    is_program,
    source_types,
)
from terricon_events_bot.domain.events import SourceEvent
from terricon_events_bot.infrastructure.terricon.client import (
    TerriconClient,
    TerriconRequestError,
)
from terricon_events_bot.infrastructure.terricon.dto import TerriconEventDTO
from terricon_events_bot.infrastructure.terricon.normalization import (
    normalize_event,
    registration_link,
)
from terricon_events_bot.infrastructure.terricon.smoke import run_smoke

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "terricon"
STAGE_URL = "https://api.example/eventsByStage"


def stage_payload(locale: Locale) -> dict[str, list[dict[str, Any]]]:
    value = json.loads((FIXTURES / f"events_by_stage_{locale.value}.json").read_text())
    assert isinstance(value, dict)
    return value


def stage_events(locale: Locale) -> dict[int, SourceEvent]:
    payload = stage_payload(locale)
    events: dict[int, SourceEvent] = {}
    for raw in (*payload["eventsNew"], *payload["eventsPreparation"]):
        event, issues = normalize_event(TerriconEventDTO.model_validate(raw), locale, None)
        assert issues == ()
        events[event.source_id] = event
    return events


def test_stage_records_without_type_text_normalize_without_warnings() -> None:
    raw = stage_payload(Locale.RU)["eventsPreparation"][0]
    assert "type_text" not in raw

    events = stage_events(Locale.RU)

    assert set(events) == {501, 901, 902, 903, 904}


def test_program_stubs_become_missing_values_and_link_to_the_site_form() -> None:
    neconf = stage_events(Locale.RU)[901]
    accelerator = stage_events(Locale.RU)[902]

    assert neconf.source_theme is SourceTheme.IT  # type=default, tags it+business
    assert neconf.event_type == "neconf"
    assert neconf.event_languages == ()
    assert neconf.address is None
    assert neconf.localization.audience is None
    assert neconf.localization.speaker is None
    assert neconf.localization.description == "Synthetic neconf description"
    assert neconf.localization.details_url == "https://terricon.kz/ru/neconf#form"
    assert neconf.registration_url == "https://terricon.kz/ru/neconf#form"
    # The links page is shown after the form is sent; it is not a registration.
    assert neconf.recording_url == "https://terricon.kz/neconf-links"

    assert accelerator.source_theme is SourceTheme.BUSINESS
    assert accelerator.localization.description is None
    assert accelerator.recording_url is None  # "/" stub
    assert accelerator.registration_url == "https://terricon.kz/ru/accelerator#form"


def test_kz_program_shares_common_fields_but_opens_its_own_page() -> None:
    ru = stage_events(Locale.RU)[901]
    kz = stage_events(Locale.KZ)[901]

    assert kz.localization.details_url == "https://terricon.kz/kz/neconf#form"
    assert kz.registration_url == ru.registration_url
    assert kz.recording_url == ru.recording_url
    assert registration_link(kz.registration_url, kz.localization.details_url) == (
        "https://terricon.kz/kz/neconf#form"
    )


def test_external_program_page_is_the_registration_and_theme_pages_keep_uid() -> None:
    events = stage_events(Locale.RU)
    partner = events[904]
    lecture = events[501]

    assert partner.source_theme is SourceTheme.IT  # default without tags
    assert partner.event_format is EventFormat.OFFLINE
    assert partner.localization.details_url == "https://partner.example/conference"
    assert partner.registration_url == "https://partner.example/conference"
    assert partner.recording_url is None

    assert lecture.source_theme is SourceTheme.MARKETING
    assert lecture.localization.details_url == "https://terricon.kz/ru/marketing#fixture20.01"
    assert lecture.registration_url == "https://registration.example/lecture"
    assert lecture.recording_url is None
    assert lecture.event_languages == ("ru",)


def test_event_kinds_split_programs_from_dated_events() -> None:
    assert event_kind("neconf") is EventKind.NECONF
    assert event_kind("hackaton") is EventKind.HACKATHON
    assert event_kind("OC") is EventKind.COURSE
    assert event_kind("umitup") is None
    assert event_kind(None) is None
    assert [is_program(value) for value in ("accelerator", "incubator", "oc", "neconf")] == [
        True,
        True,
        True,
        False,
    ]
    assert source_types(EventKind.COURSE) == ("course", "oc")
    assert stage_events(Locale.RU)[903].event_type == "oc"


@pytest.mark.asyncio
async def test_fetch_stage_reads_both_groups_without_theme_or_cache_buster() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=stage_payload(Locale.KZ), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TerriconClient(
            "https://api.example/getAllEvents", http_client, stage_url=STAGE_URL
        )
        payload = await client.fetch_stage(Locale.KZ)

    assert len(requests) == 1
    assert str(requests[0].url) == f"{STAGE_URL}?lang=kz"
    assert {event.source_id for event in payload.events} == {501, 901, 902, 903, 904}
    assert payload.observed_source_ids == frozenset({501, 901, 902, 903, 904})
    assert payload.invalid_records == 0


@pytest.mark.asyncio
async def test_fetch_stage_rejects_a_list_shaped_response_and_needs_a_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TerriconClient(
            "https://api.example/getAllEvents", http_client, stage_url=STAGE_URL, max_retries=0
        )
        with pytest.raises(TerriconRequestError) as caught:
            await client.fetch_stage(Locale.RU)
        disabled = TerriconClient("https://api.example/getAllEvents", http_client)
        with pytest.raises(TerriconRequestError) as missing:
            await disabled.fetch_stage(Locale.RU)

    assert caught.value.code == "invalid_response_shape"
    assert missing.value.code == "stage_disabled"


@pytest.mark.asyncio
async def test_smoke_checks_stage_endpoints_when_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        locale = request.url.params["lang"]
        if request.url.path.endswith("eventsByStage"):
            payload: object = stage_payload(Locale(locale))
        else:
            theme = request.url.params["theme"]
            payload = json.loads((FIXTURES / f"{locale}_{theme}.json").read_text())
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        results = await run_smoke(
            "https://api.example/getAllEvents", http_client, stage_url=STAGE_URL, max_retries=0
        )

    assert len(results) == 8
    stage = [result for result in results if result.theme is None]
    assert [result.records for result in stage] == [5, 5]
    assert all(result.succeeded for result in results)
