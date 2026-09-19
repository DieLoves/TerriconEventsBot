import pytest

from terricon_events_bot.application.broadcasts import BroadcastError, split_broadcast_text


def test_broadcast_text_split_preserves_content_with_telegram_safe_chunks() -> None:
    body = f"{'word ' * 900}\n{'x' * 4100}"

    chunks = split_broadcast_text(body)

    assert len(chunks) >= 3
    assert all(0 < len(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks).replace(" ", "").replace("\n", "") == body.replace(" ", "").replace(
        "\n", ""
    )


def test_broadcast_text_split_rejects_empty_body_and_invalid_limit() -> None:
    with pytest.raises(BroadcastError):
        split_broadcast_text("  ")
    with pytest.raises(ValueError):
        split_broadcast_text("body", limit=4097)
