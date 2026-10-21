import pytest
from pydantic import ValidationError

from terricon_events_bot.domain.ai import ClassificationOutput, TranslationInput
from terricon_events_bot.domain.enums import CategorySlug, Locale


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


def test_translation_requires_different_locales() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        TranslationInput(
            source_locale=Locale.RU,
            target_locale=Locale.RU,
            title="Title",
        )
