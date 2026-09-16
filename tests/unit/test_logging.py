import json
import logging

from terricon_events_bot.infrastructure.logging import JsonFormatter, Redactor


def test_formatter_redacts_secrets_telegram_ids_and_sensitive_fields() -> None:
    secret = "super-secret-value"
    formatter = JsonFormatter(Redactor([secret]))
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="failed for 123456789 with %s",
        args=(secret,),
        exc_info=None,
    )
    record.telegram_id = 123456789
    record.feedback_text = "private message"

    payload = json.loads(formatter.format(record))

    serialized = json.dumps(payload)
    assert secret not in serialized
    assert "123456789" not in serialized
    assert "private message" not in serialized
    assert payload["telegram_id"] == "[REDACTED]"
    assert payload["feedback_text"] == "[REDACTED]"


def test_formatter_redacts_bot_token_pattern_without_registration() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="token 123456789:abcdefghijklmnopqrstuvwxyz_123456",
        args=(),
        exc_info=None,
    )

    assert "abcdefghijklmnopqrstuvwxyz" not in formatter.format(record)
