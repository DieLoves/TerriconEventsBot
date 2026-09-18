from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from terricon_events_bot.infrastructure.terricon.smoke import run_smoke

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "terricon"


@pytest.mark.asyncio
async def test_smoke_requests_exactly_six_themed_endpoints_without_database() -> None:
    requests: list[tuple[str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        locale = request.url.params.get("lang")
        theme = request.url.params.get("theme")
        requests.append((locale, theme))
        fixture = FIXTURES / f"{locale}_{theme}.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        results = await run_smoke(
            "https://api.example/events",
            http_client,
            max_retries=0,
        )

    assert len(results) == 6
    assert all(result.succeeded and result.records == 1 for result in results)
    assert set(requests) == {
        (locale, theme) for locale in ("ru", "kz") for theme in ("it", "business", "marketing")
    }
    assert all(locale is not None and theme is not None for locale, theme in requests)


@pytest.mark.asyncio
async def test_smoke_fails_a_nonempty_fully_invalid_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        locale = request.url.params["lang"]
        theme = request.url.params["theme"]
        fixture = FIXTURES / f"{locale}_{theme}.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        if (locale, theme) == ("ru", "it"):
            payload[0]["card_info"]["name"] = ""
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        results = await run_smoke("https://api.example/events", http_client, max_retries=0)

    failed = [result for result in results if not result.succeeded]
    assert len(failed) == 1
    assert failed[0].error_code == "all_records_invalid"
