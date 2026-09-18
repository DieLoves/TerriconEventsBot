from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from terricon_events_bot.domain.enums import Locale, SourceTheme
from terricon_events_bot.infrastructure.terricon.client import (
    TerriconClient,
    TerriconRequestError,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "terricon" / "ru_it.json"


def fixture_payload() -> list[dict[str, Any]]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def make_client(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    **kwargs: Any,
) -> tuple[TerriconClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TerriconClient("https://api.example/events", http_client, **kwargs)
    return client, http_client


@pytest.mark.asyncio
async def test_fetch_retries_timeout_and_server_error_with_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "2"}, request=request)
        if calls == 2:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=fixture_payload(), request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client, http_client = make_client(handler, sleep=sleep, random_uniform=lambda _a, _b: 0.0)
    try:
        payload = await client.fetch_detailed(Locale.RU, SourceTheme.IT)
    finally:
        await http_client.aclose()

    assert calls == 3
    assert delays == [2.0, 2.0]
    assert payload.attempts == 3
    assert [event.source_id for event in payload.events] == [101]


@pytest.mark.asyncio
async def test_fetch_honors_initial_request_plus_three_retries() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client, http_client = make_client(handler, sleep=sleep, random_uniform=lambda _a, _b: 0.0)
    try:
        with pytest.raises(TerriconRequestError) as caught:
            await client.fetch(Locale.RU, SourceTheme.IT)
    finally:
        await http_client.aclose()

    assert calls == 4
    assert delays == [1.0, 2.0, 4.0]
    assert caught.value.attempts == 4
    assert caught.value.code == "http_503"


@pytest.mark.asyncio
async def test_fetch_honors_http_date_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "Tue, 01 Jan 2030 00:00:05 GMT"},
                request=request,
            )
        return httpx.Response(200, json=fixture_payload(), request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client, http_client = make_client(
        handler,
        sleep=sleep,
        random_uniform=lambda _a, _b: 0.0,
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    try:
        await client.fetch(Locale.RU, SourceTheme.IT)
    finally:
        await http_client.aclose()

    assert delays == [5.0]


@pytest.mark.asyncio
async def test_fetch_does_not_retry_non_retryable_client_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    client, http_client = make_client(handler)
    try:
        with pytest.raises(TerriconRequestError) as caught:
            await client.fetch(Locale.KZ, SourceTheme.MARKETING)
    finally:
        await http_client.aclose()

    assert calls == 1
    assert caught.value.code == "http_404"


@pytest.mark.asyncio
async def test_invalid_records_are_skipped_but_their_valid_ids_remain_observed() -> None:
    valid = fixture_payload()[0]
    invalid = deepcopy(valid)
    invalid["id"] = 102
    invalid["card_info"]["name"] = ""
    response_payload: list[Any] = [valid, invalid, "not-a-record"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload, request=request)

    client, http_client = make_client(handler)
    try:
        payload = await client.fetch_detailed(Locale.RU, SourceTheme.IT)
    finally:
        await http_client.aclose()

    assert [event.source_id for event in payload.events] == [101]
    assert payload.observed_source_ids == {101, 102}
    assert payload.invalid_records == 2


@pytest.mark.asyncio
async def test_nonempty_fully_invalid_payload_fails_endpoint() -> None:
    invalid = fixture_payload()[0]
    invalid["card_info"]["name"] = ""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[invalid, "not-a-record"], request=request)

    client, http_client = make_client(handler)
    try:
        with pytest.raises(TerriconRequestError) as caught:
            await client.fetch_detailed(Locale.RU, SourceTheme.IT)
    finally:
        await http_client.aclose()

    assert caught.value.code == "all_records_invalid"
    assert caught.value.attempts == 1


@pytest.mark.asyncio
async def test_empty_payload_remains_a_valid_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    client, http_client = make_client(handler)
    try:
        payload = await client.fetch_detailed(Locale.RU, SourceTheme.IT)
    finally:
        await http_client.aclose()

    assert payload.events == ()
    assert payload.invalid_records == 0


@pytest.mark.asyncio
async def test_response_body_and_retry_after_are_bounded() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": "86400"},
                request=request,
            )
        return httpx.Response(200, content=b"[123456789]", request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client, http_client = make_client(
        handler,
        max_retries=1,
        max_response_bytes=8,
        max_retry_delay_seconds=30,
        sleep=sleep,
        random_uniform=lambda _a, _b: 0.0,
    )
    try:
        with pytest.raises(TerriconRequestError) as caught:
            await client.fetch_detailed(Locale.RU, SourceTheme.IT)
    finally:
        await http_client.aclose()

    assert delays == [30]
    assert caught.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_requests_share_a_bounded_concurrency_semaphore() -> None:
    active = 0
    maximum_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=fixture_payload(), request=request)

    client, http_client = make_client(handler, max_concurrency=2)
    try:
        await asyncio.gather(
            *(client.fetch(locale, theme) for locale in Locale for theme in SourceTheme)
        )
    finally:
        await http_client.aclose()

    assert maximum_active == 2
