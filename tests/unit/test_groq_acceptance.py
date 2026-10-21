import json
from pathlib import Path

import pytest

from terricon_events_bot.infrastructure.groq_acceptance import load_cases, score_categories

DATA = Path("tests/fixtures/groq/stage10-real-events.json")


def test_real_cases_are_frozen_and_cover_both_locales_and_other():
    cases = load_cases(DATA)
    assert len(cases) == 24
    assert len({c["source_id"] for c in cases}) == 24
    assert {"ru", "kz"} == {loc["locale"] for c in cases for loc in c["input"]["localizations"]}
    assert any(c["required_categories"] == ["other"] for c in cases)
    assert any(len(c["required_categories"]) > 1 for c in cases)


def test_score_rejects_missing_primary_and_spurious_categories():
    case = {"required_categories": ["ai_data"], "allowed_categories": ["ai_data", "development_it"]}
    assert score_categories(case, ["ai_data", "development_it"])["passed"] is True
    assert score_categories(case, ["ai_data", "marketing_sales"])["passed"] is False
    assert score_categories(case, ["development_it"])["missing"] == ["ai_data"]


def test_load_cases_rejects_invalid_input_before_any_external_request(tmp_path):
    data = json.loads(DATA.read_text())
    data["cases"][0]["input"]["localizations"][0]["locale"] = "en"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_cases(path)


def test_acceptance_fingerprint_changes_with_prompt_and_output_allowance(monkeypatch):
    from terricon_events_bot.infrastructure import groq_adapter as module

    first = module.groq_behavior_fingerprint()
    monkeypatch.setattr(module, "CLASSIFICATION_PROMPT_VERSION", "changed")
    assert first != module.groq_behavior_fingerprint()
    second = module.groq_behavior_fingerprint()
    monkeypatch.setattr(module, "_MAX_CLASSIFICATION_OUTPUT_TOKENS", 17)
    assert second != module.groq_behavior_fingerprint()
