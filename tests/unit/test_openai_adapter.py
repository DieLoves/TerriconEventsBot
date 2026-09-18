from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import httpx
import openai
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from terricon_events_bot.domain.ai import (
    ClassificationInput,
    ClassificationLocalization,
    ClassificationOutput,
    OpenAIUsageData,
    TranslationInput,
)
from terricon_events_bot.domain.enums import CategorySlug, Locale
from terricon_events_bot.infrastructure.openai_adapter import (
    CLASSIFICATION_PROMPT_VERSION,
    OpenAIAdapterError,
    OpenAIEventAdapter,
)


class FakeResponses:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def fake_client(responses: FakeResponses) -> AsyncOpenAI:
    return cast(AsyncOpenAI, SimpleNamespace(responses=responses))


def response(parsed: object, *, refusal: bool = False) -> object:
    content = [SimpleNamespace(type="refusal")] if refusal else []
    return SimpleNamespace(
        output_parsed=parsed,
        output=[SimpleNamespace(content=content)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=8),
    )


def classification_input() -> ClassificationInput:
    return ClassificationInput(
        localizations=(
            ClassificationLocalization(
                locale=Locale.RU,
                title="Python meetup",
                description="Async Python",
            ),
        )
    )


def test_classification_output_enforces_category_contract() -> None:
    assert ClassificationOutput(categories=["ai_data"]).categories == (CategorySlug.AI_DATA,)

    with pytest.raises(ValidationError, match="cannot be combined"):
        ClassificationOutput(categories=["other", "ai_data"])
    with pytest.raises(ValidationError, match="unique"):
        ClassificationOutput(categories=["ai_data", "ai_data"])
    with pytest.raises(ValidationError):
        ClassificationOutput(categories=[])

    schema = ClassificationOutput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["categories"]["minItems"] == 1  # type: ignore[index]
    assert schema["properties"]["categories"]["maxItems"] == 3  # type: ignore[index]


@pytest.mark.asyncio
async def test_classify_uses_responses_structured_output_without_storage() -> None:
    api = FakeResponses(response({"categories": ["development_it", "ai_data"]}))
    ticks = iter((10.0, 10.125))
    adapter = OpenAIEventAdapter(fake_client(api), "gpt-5.6-luna", timer=lambda: next(ticks))

    result = await adapter.classify_event(classification_input())

    assert result.output.categories == (CategorySlug.DEVELOPMENT_IT, CategorySlug.AI_DATA)
    assert result.usage.input_tokens == 120
    assert result.duration_ms == 125
    assert result.prompt_version == CLASSIFICATION_PROMPT_VERSION
    assert api.calls[0]["model"] == "gpt-5.6-luna"
    assert api.calls[0]["text_format"] is ClassificationOutput
    assert api.calls[0]["reasoning"] == {"effort": "low"}
    assert api.calls[0]["max_output_tokens"] == 4096
    assert api.calls[0]["store"] is False


@pytest.mark.asyncio
async def test_classify_rejects_refusal_invalid_output_and_timeout() -> None:
    cases: list[tuple[FakeResponses, str]] = [
        (FakeResponses(response(None, refusal=True)), "refusal"),
        (FakeResponses(response({"categories": ["unknown"]})), "invalid_output"),
        (
            FakeResponses(
                error=openai.APITimeoutError(request=httpx.Request("POST", "https://api.test"))
            ),
            "timeout",
        ),
    ]
    for api, expected_code in cases:
        adapter = OpenAIEventAdapter(fake_client(api), "gpt-5.6-luna")
        with pytest.raises(OpenAIAdapterError) as caught:
            await adapter.classify_event(classification_input())
        assert caught.value.code == expected_code
        if expected_code in {"refusal", "invalid_output"}:
            assert caught.value.usage == OpenAIUsageData(input_tokens=120, output_tokens=8)
        else:
            assert caught.value.usage is None


@pytest.mark.asyncio
async def test_translate_uses_a_separate_strict_contract() -> None:
    api = FakeResponses(
        response(
            {
                "title": "Python кездесуі",
                "description": "Async Python",
                "audience": None,
                "speaker": "Ada",
            }
        )
    )
    adapter = OpenAIEventAdapter(fake_client(api), "gpt-5.6-luna")

    result = await adapter.translate_missing_locale(
        TranslationInput(
            source_locale=Locale.RU,
            target_locale=Locale.KZ,
            title="Python meetup",
            description="Async Python",
            audience=None,
            speaker="Ada",
        )
    )

    assert result.output.title == "Python кездесуі"
    assert api.calls[0]["text_format"].__name__ == "TranslationOutput"  # type: ignore[union-attr]
    assert api.calls[0]["max_output_tokens"] == 32768


@pytest.mark.asyncio
async def test_missing_parsed_output_is_reported_as_invalid_output() -> None:
    api = FakeResponses(SimpleNamespace(output=None, usage=None))
    adapter = OpenAIEventAdapter(fake_client(api), "gpt-5.6-luna")

    with pytest.raises(OpenAIAdapterError, match="invalid_output"):
        await adapter.classify_event(classification_input())


def test_translation_requires_different_locales() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        TranslationInput(
            source_locale=Locale.RU,
            target_locale=Locale.RU,
            title="Title",
        )
