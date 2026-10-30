from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage

from terricon_events_bot.application.delivery import EventButton, TelegramSendError
from terricon_events_bot.telegram.delivery import AiogramTelegramGateway


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("telegram_error", "code", "transient", "blocked", "retry_after"),
    (
        (
            TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "forbidden"),
            "telegram_forbidden",
            False,
            True,
            None,
        ),
        (
            TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "later", 12),
            "telegram_retry_after",
            True,
            False,
            timedelta(seconds=12),
        ),
        (
            TelegramNetworkError(SendMessage(chat_id=1, text="x"), "network"),
            "telegram_transient",
            True,
            False,
            None,
        ),
        (
            TelegramBadRequest(SendMessage(chat_id=1, text="x"), "bad"),
            "telegram_permanent",
            False,
            False,
            None,
        ),
    ),
)
async def test_aiogram_gateway_classifies_delivery_errors(
    telegram_error: Exception,
    code: str,
    transient: bool,
    blocked: bool,
    retry_after: timedelta | None,
) -> None:
    bot = Mock(spec=Bot)
    bot.send_message = AsyncMock(side_effect=telegram_error)
    gateway = AiogramTelegramGateway(cast(Any, bot))

    with pytest.raises(TelegramSendError) as captured:
        await gateway.send_message(100, "message")

    assert captured.value.code == code
    assert captured.value.transient is transient
    assert captured.value.blocked is blocked
    assert captured.value.retry_after == retry_after


@pytest.mark.asyncio
async def test_retry_after_is_bounded_before_timedelta_conversion() -> None:
    bot = Mock(spec=Bot)
    bot.send_message = AsyncMock(
        side_effect=TelegramRetryAfter(
            SendMessage(chat_id=1, text="x"),
            "later",
            10**100,
        )
    )
    gateway = AiogramTelegramGateway(cast(Any, bot))

    with pytest.raises(TelegramSendError) as captured:
        await gateway.send_message(100, "message")

    assert captured.value.retry_after == timedelta(days=1)


@pytest.mark.asyncio
async def test_gateway_attaches_event_buttons_only_when_present() -> None:
    bot = Mock(spec=Bot)
    bot.send_message = AsyncMock(return_value=Mock(message_id=7))
    gateway = AiogramTelegramGateway(cast(Any, bot))

    await gateway.send_message(100, "digest", (EventButton(42, "📋 Открыть в боте"),))
    await gateway.send_message(100, "plain")

    keyboard = bot.send_message.await_args_list[0].kwargs["reply_markup"]
    assert [
        [(item.text, item.callback_data) for item in row] for row in keyboard.inline_keyboard
    ] == [[("📋 Открыть в боте", "ne:42")]]
    assert bot.send_message.await_args_list[1].kwargs["reply_markup"] is None
