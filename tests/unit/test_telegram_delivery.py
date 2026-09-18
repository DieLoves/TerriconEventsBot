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

from terricon_events_bot.application.delivery import TelegramSendError
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
