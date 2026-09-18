from decimal import Decimal

import pytest

from terricon_events_bot.application.classification import (
    OpenAIPricing,
    calculate_openai_cost,
)
from terricon_events_bot.domain.ai import OpenAIUsageData


def test_openai_cost_preserves_sub_microdollar_precision() -> None:
    cost = calculate_openai_cost(
        OpenAIUsageData(input_tokens=1, output_tokens=1),
        OpenAIPricing(
            input_per_million_usd=Decimal("0.20"),
            output_per_million_usd=Decimal("1.20"),
        ),
    )

    assert cost == Decimal("0.000001400")


def test_openai_pricing_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        OpenAIPricing(Decimal("-0.01"), Decimal("1.20"))


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_openai_pricing_rejects_non_finite_values(value: Decimal) -> None:
    with pytest.raises(ValueError, match="finite"):
        OpenAIPricing(value, Decimal("1.20"))


@pytest.mark.parametrize("tokens", [-1, True, 1.5])
def test_openai_usage_rejects_invalid_token_counts(tokens: object) -> None:
    with pytest.raises(ValueError, match="nonnegative integers"):
        OpenAIUsageData(input_tokens=tokens, output_tokens=1)  # type: ignore[arg-type]
