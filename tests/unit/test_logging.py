import json
import logging

from terricon_events_bot.infrastructure.logging import JsonFormatter, Redactor


class ExternalValue:
    def __str__(self) -> str:
        return "value with object-secret"


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


def test_formatter_recursively_redacts_nested_structures() -> None:
    secret = "nested-super-secret"
    formatter = JsonFormatter(Redactor([secret]))
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="request failed",
        args=(),
        exc_info=None,
    )
    record.context = {
        "request": {
            "telegram_id": 123456789,
            "items": [secret, "owner=987654321", {"message_text": "private message"}],
        },
        111222333: "numeric mapping key",
    }

    payload = json.loads(formatter.format(record))
    serialized = json.dumps(payload)

    assert secret not in serialized
    assert "123456789" not in serialized
    assert "987654321" not in serialized
    assert "111222333" not in serialized
    assert "private message" not in serialized
    assert payload["context"]["request"]["telegram_id"] == "[REDACTED]"


def test_formatter_handles_cyclic_structures_without_leaking_values() -> None:
    context: dict[str, object] = {"feedback_text": "private message"}
    context["self"] = context
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="cyclic context",
        args=(),
        exc_info=None,
    )
    record.context = context

    payload = json.loads(formatter.format(record))

    assert payload["context"]["feedback_text"] == "[REDACTED]"
    assert payload["context"]["self"] == "[REDACTED_CYCLE]"


def test_formatter_redacts_string_representation_of_unknown_objects() -> None:
    formatter = JsonFormatter(Redactor(["object-secret"]))
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unknown object",
        args=(),
        exc_info=None,
    )
    record.context = {"value": ExternalValue()}

    serialized = formatter.format(record)

    assert "object-secret" not in serialized
    assert "[REDACTED_SECRET]" in serialized


def test_debug_logging_does_not_expose_groq_request_content(capsys: object) -> None:
    import httpx
    from groq import Groq

    from terricon_events_bot.infrastructure.logging import configure_logging

    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    sdk_logger = logging.getLogger("groq._base_client")
    previous_sdk_level = sdk_logger.level
    try:
        configure_logging("DEBUG")
        sdk_logger.setLevel(logging.DEBUG)
        with Groq(
            api_key="test-key",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        json={
                            "id": "test",
                            "choices": [],
                            "model": "openai/gpt-oss-120b",
                            "object": "chat.completion",
                            "created": 0,
                        },
                    )
                )
            ),
        ) as client:
            client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": "private event title in request"}],
            )
        logging.getLogger("terricon_events_bot").debug("application debug is retained")
        captured = capsys.readouterr()  # type: ignore[attr-defined]
        assert "private event title" not in captured.err
        assert "application debug is retained" in captured.err
    finally:
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
        sdk_logger.setLevel(previous_sdk_level)
