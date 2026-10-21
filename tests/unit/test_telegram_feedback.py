from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot

from terricon_events_bot.application.feedback import FeedbackNotification
from terricon_events_bot.domain.enums import FeedbackKind, Locale
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.feedback import AiogramFeedbackGateway

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_feedback_gateway_copies_content_without_forwarding_or_html() -> None:
    bot = Mock(spec=Bot)
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=40))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=41))
    localizations = LocalizationCatalog.load(PROJECT_ROOT / "locales")
    gateway = AiogramFeedbackGateway(cast(Any, bot), -100500, localizations)
    notification = FeedbackNotification(
        ticket_id=7,
        user_id=1,
        telegram_id=100,
        locale=Locale.RU,
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

    await gateway.send_user_reply(100, Locale.KZ, "Reply body")
    await gateway.notify_closed(100, Locale.KZ)

    assert bot.send_message.await_args_list[-2].kwargs["text"] == (
        "Ответ администратора:\n\nReply body"
    )
    assert bot.send_message.await_args_list[-1].kwargs["text"] == "Обращение закрыто."
