from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot

from terricon_events_bot.application.feedback import FeedbackNotification
from terricon_events_bot.domain.enums import FeedbackKind
from terricon_events_bot.telegram.feedback import AiogramFeedbackGateway


@pytest.mark.asyncio
async def test_feedback_gateway_copies_content_without_forwarding_or_html() -> None:
    bot = Mock(spec=Bot)
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=40))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=41))
    gateway = AiogramFeedbackGateway(cast(Any, bot), -100500)
    notification = FeedbackNotification(
        ticket_id=7,
        user_id=1,
        telegram_id=100,
        kind=FeedbackKind.ERROR,
        display_name="<Admin>",
        username="member",
        body="x" * 4000,
        telegram_file_id="photo-id",
    )

    result = await gateway.publish(notification)

    assert result == (-100500, 41)
    bot.send_photo.assert_awaited_once_with(chat_id=-100500, photo="photo-id")
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == -100500
    assert len(call.kwargs["text"]) <= 4096
    assert "parse_mode" not in call.kwargs
    assert "<Admin>" in call.kwargs["text"]
    assert not hasattr(bot, "forward_message") or bot.forward_message.call_count == 0
