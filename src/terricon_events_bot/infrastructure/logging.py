from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

_TOKEN_PATTERN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_TELEGRAM_ID_PATTERN = re.compile(r"(?<!\d)\d{5,15}(?!\d)")
_SENSITIVE_KEYS = {
    "api_key",
    "bot_token",
    "feedback_text",
    "message_text",
    "openai_api_key",
    "telegram_id",
    "token",
    "user_id",
}
_STANDARD_LOG_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__)


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def text(self, value: object) -> str:
        result = str(value)
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED_SECRET]")
        result = _TOKEN_PATTERN.sub("[REDACTED_TOKEN]", result)
        return _TELEGRAM_ID_PATTERN.sub("[REDACTED_ID]", result)

    def value(self, key: str, value: Any) -> Any:
        if key.lower() in _SENSITIVE_KEYS:
            return "[REDACTED]"
        if isinstance(value, str):
            return self.text(value)
        return value


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or Redactor()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self._redactor.text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_KEYS and not key.startswith("_"):
                payload[key] = self._redactor.value(key, value)
        if record.exc_info:
            payload["exception"] = self._redactor.text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(Redactor(secrets)))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
