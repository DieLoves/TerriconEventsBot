from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from terricon_events_bot.domain.enums import CategorySlug, Locale


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClassificationLocalization(_StrictModel):
    locale: Locale
    title: str = Field(min_length=1)
    description: str | None = None
    audience: str | None = None
    speaker: str | None = None


class ClassificationInput(_StrictModel):
    localizations: tuple[ClassificationLocalization, ...] = Field(min_length=1, max_length=2)


class ClassificationOutput(_StrictModel):
    categories: tuple[CategorySlug, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_categories(self) -> ClassificationOutput:
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("Classification categories must be unique")
        if CategorySlug.OTHER in self.categories and len(self.categories) != 1:
            raise ValueError("The other category cannot be combined with other categories")
        return self


class TranslationInput(_StrictModel):
    source_locale: Locale
    target_locale: Locale
    title: str = Field(min_length=1)
    description: str | None = None
    audience: str | None = None
    speaker: str | None = None

    @model_validator(mode="after")
    def validate_distinct_locales(self) -> TranslationInput:
        if self.source_locale is self.target_locale:
            raise ValueError("Translation source and target locales must differ")
        return self


class TranslationOutput(_StrictModel):
    title: str = Field(min_length=1)
    description: str | None
    audience: str | None
    speaker: str | None


@dataclass(frozen=True, slots=True)
class OpenAIUsageData:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.input_tokens, int)
            or isinstance(self.input_tokens, bool)
            or self.input_tokens < 0
            or not isinstance(self.output_tokens, int)
            or isinstance(self.output_tokens, bool)
            or self.output_tokens < 0
        ):
            raise ValueError("OpenAI token counts must be nonnegative integers")


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    output: ClassificationOutput
    usage: OpenAIUsageData
    model: str
    prompt_version: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class TranslationResult:
    output: TranslationOutput
    usage: OpenAIUsageData
    model: str
    prompt_version: str
    duration_ms: int
