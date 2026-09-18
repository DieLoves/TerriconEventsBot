from terricon_events_bot.domain.event_changes import (
    important_hash,
    semantic_hash,
    structured_diff,
)


def test_semantic_hash_ignores_urls_but_changes_with_classification_text() -> None:
    first = {
        "ru": {
            "title": "Title",
            "description": "Description",
            "audience": "Audience",
            "speaker": "Speaker",
            "details_url": "https://example.test/one",
        }
    }
    second = {"ru": {**first["ru"], "details_url": "https://example.test/two"}}

    assert semantic_hash(first) == semantic_hash(second)

    second["ru"]["description"] = "Changed"
    assert semantic_hash(first) != semantic_hash(second)


def test_important_hash_is_order_independent_and_diff_is_structured() -> None:
    old = {"starts_at": "2030-01-01T10:00:00+00:00", "format": "online"}
    reordered = {"format": "online", "starts_at": "2030-01-01T10:00:00+00:00"}
    new = {"starts_at": "2030-01-02T10:00:00+00:00", "format": "online"}

    assert important_hash(old) == important_hash(reordered)
    assert structured_diff(old, new) == {
        "starts_at": {
            "old": "2030-01-01T10:00:00+00:00",
            "new": "2030-01-02T10:00:00+00:00",
        }
    }
