from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import ValidationError

from terricon_events_bot.domain.enums import Locale, SourceTheme
from terricon_events_bot.domain.events import SourceEvent
from terricon_events_bot.infrastructure.terricon.dto import TerriconEventDTO
from terricon_events_bot.infrastructure.terricon.normalization import (
    NormalizationError,
    normalize_event,
)

Sleep = Callable[[float], Awaitable[None]]
RandomUniform = Callable[[float, float], float]
Clock = Callable[[], datetime]

_DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_DEFAULT_MAX_RETRY_DELAY_SECONDS = 60.0


class _ResponseTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EndpointPayload:
    events: tuple[SourceEvent, ...]
    observed_source_ids: frozenset[int]
    invalid_records: int
    attempts: int


class TerriconRequestError(RuntimeError):
    def __init__(self, code: str, attempts: int, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.attempts = attempts
        self.status_code = status_code


class TerriconClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        max_concurrency: int = 3,
        sleep: Sleep = asyncio.sleep,
        random_uniform: RandomUniform = random.uniform,
        clock: Clock = lambda: datetime.now(UTC),
        logger: logging.Logger | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_retry_delay_seconds: float = _DEFAULT_MAX_RETRY_DELAY_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if max_retry_delay_seconds <= 0:
            raise ValueError("max_retry_delay_seconds must be positive")
        self._base_url = base_url
        self._client = client
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._max_response_bytes = max_response_bytes
        self._max_retry_delay_seconds = max_retry_delay_seconds

    async def fetch(self, locale: Locale, theme: SourceTheme) -> list[SourceEvent]:
        payload = await self.fetch_detailed(locale, theme)
        return list(payload.events)

    async def fetch_detailed(self, locale: Locale, theme: SourceTheme) -> EndpointPayload:
        raw_records, attempts = await self._fetch_records(locale, theme)
        events: list[SourceEvent] = []
        observed_source_ids: set[int] = set()
        invalid_records = 0

        for index, raw in enumerate(raw_records):
            if isinstance(raw, dict):
                raw_id = raw.get("id")
                if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
                    observed_source_ids.add(raw_id)
            try:
                dto = TerriconEventDTO.model_validate(raw)
                event, issues = normalize_event(dto, locale, theme)
            except (ValidationError, NormalizationError) as error:
                invalid_records += 1
                self._logger.warning(
                    "Skipping invalid Terricon record",
                    extra={
                        "error_code": "invalid_record",
                        "locale": locale.value,
                        "source_theme": theme.value,
                        "record_index": index,
                        "validation_error": type(error).__name__,
                    },
                )
                continue
            events.append(event)
            for issue in issues:
                self._logger.warning(
                    "Normalized unknown Terricon value",
                    extra={
                        "error_code": issue.code,
                        "field": issue.field,
                        "locale": locale.value,
                        "source_id": event.source_id,
                        "source_theme": theme.value,
                        "source_value": issue.value[:128],
                    },
                )

        if raw_records and not events:
            raise TerriconRequestError("all_records_invalid", attempts)

        return EndpointPayload(
            events=tuple(events),
            observed_source_ids=frozenset(observed_source_ids),
            invalid_records=invalid_records,
            attempts=attempts,
        )

    async def _fetch_records(self, locale: Locale, theme: SourceTheme) -> tuple[list[Any], int]:
        attempts_limit = self._max_retries + 1
        for attempt in range(1, attempts_limit + 1):
            response: httpx.Response | None = None
            error_code: str | None = None
            try:
                async with self._semaphore:
                    async with self._client.stream(
                        "GET",
                        self._base_url,
                        params={"lang": locale.value, "theme": theme.value},
                        timeout=self._timeout,
                    ) as response:
                        if 200 <= response.status_code < 300:
                            try:
                                payload = await self._read_json(response)
                            except _ResponseTooLargeError:
                                error_code = "response_too_large"
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                error_code = "invalid_json"
                            else:
                                if isinstance(payload, list):
                                    return payload, attempt
                                error_code = "invalid_response_shape"
                        elif response.status_code == 429 or response.status_code >= 500:
                            error_code = f"http_{response.status_code}"
                        else:
                            raise TerriconRequestError(
                                f"http_{response.status_code}", attempt, response.status_code
                            )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.DecodingError) as error:
                error_code = "timeout" if isinstance(error, httpx.TimeoutException) else "network"

            if attempt >= attempts_limit:
                status_code = response.status_code if response is not None else None
                raise TerriconRequestError(error_code or "request_failed", attempt, status_code)
            await self._sleep(self._retry_delay(attempt, response))

        raise AssertionError("retry loop must return or raise")

    def _retry_delay(self, failed_attempt: int, response: httpx.Response | None) -> float:
        exponential = 2.0 ** (failed_attempt - 1)
        delay = exponential + self._random_uniform(0.0, exponential * 0.25)
        if response is not None:
            retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                delay = max(delay, retry_after)
        return min(delay, self._max_retry_delay_seconds)

    async def _read_json(self, response: httpx.Response) -> Any:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > self._max_response_bytes:
                raise _ResponseTooLargeError

        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self._max_response_bytes:
                raise _ResponseTooLargeError
            body.extend(chunk)
        return json.loads(body)

    def _parse_retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        candidate = value.strip()
        try:
            seconds = float(candidate)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(candidate)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = (retry_at - self._clock()).total_seconds()
        return max(0.0, seconds)
