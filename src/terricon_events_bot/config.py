from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AliasChoices,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from terricon_events_bot.domain.enums import AccessMode

TelegramIds = Annotated[frozenset[int], NoDecode]


def _parse_telegram_ids(value: Any) -> Any:
    if isinstance(value, str):
        if not value.strip():
            return frozenset()
        values = value.split(",")
        if any(not item.strip().isdigit() for item in values):
            raise ValueError("Telegram IDs must be comma-separated positive integers")
        return frozenset(int(item.strip()) for item in values)
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    base_url: HttpUrl = Field(validation_alias="BASE_URL")
    telegram_bot_token: SecretStr = Field(
        min_length=1,
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
    )
    openai_api_key: SecretStr = Field(min_length=1, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(
        default="gpt-5.6-luna",
        min_length=1,
        max_length=128,
        validation_alias="OPENAI_MODEL",
    )
    openai_monthly_budget_usd: Decimal = Field(
        default=Decimal("5"),
        ge=0,
        allow_inf_nan=False,
        validation_alias="OPENAI_MONTHLY_BUDGET_USD",
    )
    openai_input_price_per_million_usd: Decimal = Field(
        default=Decimal("0.20"),
        ge=0,
        allow_inf_nan=False,
        validation_alias="OPENAI_INPUT_PRICE_PER_MILLION_USD",
    )
    openai_output_price_per_million_usd: Decimal = Field(
        default=Decimal("1.20"),
        ge=0,
        allow_inf_nan=False,
        validation_alias="OPENAI_OUTPUT_PRICE_PER_MILLION_USD",
    )
    openai_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
        allow_inf_nan=False,
        validation_alias="OPENAI_TIMEOUT_SECONDS",
    )
    database_url: SecretStr = Field(min_length=1, validation_alias="DATABASE_URL")
    admin_telegram_ids: TelegramIds = Field(repr=False, validation_alias="ADMIN_TELEGRAM_IDS")
    access_mode: AccessMode = Field(default=AccessMode.ALLOWLIST, validation_alias="ACCESS_MODE")
    allowed_telegram_ids: TelegramIds = Field(
        default_factory=frozenset, repr=False, validation_alias="ALLOWED_TELEGRAM_IDS"
    )
    app_timezone: str = Field(default="Asia/Almaty", validation_alias="APP_TIMEZONE")
    sync_interval_hours: int = Field(
        default=6, ge=1, le=24 * 7, validation_alias="SYNC_INTERVAL_HOURS"
    )
    sync_max_retries: int = Field(default=3, ge=0, le=10, validation_alias="SYNC_MAX_RETRIES")
    events_page_size: int = Field(default=5, ge=1, le=20, validation_alias="EVENTS_PAGE_SIZE")
    quiet_hours_start: time = Field(default=time(22), validation_alias="QUIET_HOURS_START")
    quiet_hours_end: time = Field(default=time(9), validation_alias="QUIET_HOURS_END")
    feedback_retention_days: int = Field(
        default=90, ge=1, le=3650, validation_alias="FEEDBACK_RETENTION_DAYS"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("admin_telegram_ids", "allowed_telegram_ids", mode="before")
    @classmethod
    def parse_telegram_ids(cls, value: Any) -> Any:
        return _parse_telegram_ids(value)

    @field_validator("openai_model", mode="before")
    @classmethod
    def normalize_openai_model(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("admin_telegram_ids", "allowed_telegram_ids")
    @classmethod
    def validate_positive_telegram_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(item <= 0 for item in value):
            raise ValueError("Telegram IDs must be positive")
        return value

    @field_validator("database_url", mode="before")
    @classmethod
    def validate_database_url(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.startswith(
            ("postgresql+asyncpg://", "postgresql://")
        ):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return value

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown timezone: {value}") from error
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL is invalid")
        return normalized

    @model_validator(mode="after")
    def validate_access(self) -> Settings:
        if not self.admin_telegram_ids:
            raise ValueError("At least one ADMIN_TELEGRAM_IDS value is required")
        if self.access_mode is AccessMode.ALLOWLIST and not self.allowed_telegram_ids:
            raise ValueError("ALLOWED_TELEGRAM_IDS is required in allowlist mode")
        return self

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)


def load_settings(env_file: str | Path = ".env") -> Settings:
    # BaseSettings accepts this runtime-only source override, but its generated
    # constructor signature exposed to mypy contains only declared settings.
    return Settings(_env_file=env_file)  # type: ignore[call-arg]
