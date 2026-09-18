from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import httpx
from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

from terricon_events_bot.domain.enums import Locale, SourceTheme
from terricon_events_bot.infrastructure.terricon.client import (
    TerriconClient,
    TerriconRequestError,
)


class SmokeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    base_url: HttpUrl = Field(validation_alias="BASE_URL")
    sync_max_retries: int = Field(default=3, ge=0, le=10, validation_alias="SYNC_MAX_RETRIES")


@dataclass(frozen=True, slots=True)
class SmokeEndpointResult:
    locale: Locale
    theme: SourceTheme
    succeeded: bool
    records: int
    invalid_records: int
    attempts: int
    error_code: str | None


async def run_smoke(
    base_url: str,
    http_client: httpx.AsyncClient,
    *,
    max_retries: int = 3,
) -> tuple[SmokeEndpointResult, ...]:
    source = TerriconClient(base_url, http_client, max_retries=max_retries)

    async def check(locale: Locale, theme: SourceTheme) -> SmokeEndpointResult:
        try:
            payload = await source.fetch_detailed(locale, theme)
        except TerriconRequestError as error:
            return SmokeEndpointResult(
                locale=locale,
                theme=theme,
                succeeded=False,
                records=0,
                invalid_records=0,
                attempts=error.attempts,
                error_code=error.code,
            )
        return SmokeEndpointResult(
            locale=locale,
            theme=theme,
            succeeded=True,
            records=len(payload.events),
            invalid_records=payload.invalid_records,
            attempts=payload.attempts,
            error_code=None,
        )

    return tuple(
        await asyncio.gather(*(check(locale, theme) for locale in Locale for theme in SourceTheme))
    )


async def _main() -> int:
    settings = SmokeSettings()  # type: ignore[call-arg]
    async with httpx.AsyncClient() as http_client:
        results = await run_smoke(
            str(settings.base_url),
            http_client,
            max_retries=settings.sync_max_retries,
        )
    for result in results:
        status = "ok" if result.succeeded else f"failed:{result.error_code}"
        print(
            f"{result.locale.value}/{result.theme.value}: {status}; "
            f"records={result.records}; invalid={result.invalid_records}; "
            f"attempts={result.attempts}"
        )
    return 0 if all(result.succeeded for result in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only smoke test for the Terricon API")
    parser.parse_args()
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
