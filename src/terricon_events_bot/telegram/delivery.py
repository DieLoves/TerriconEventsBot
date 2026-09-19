from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from terricon_events_bot.application.delivery import TelegramSendError


class AiogramTelegramGateway:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(self, telegram_id: int, text: str) -> int:
        try:
            message = await self._bot.send_message(chat_id=telegram_id, text=text)
        except TelegramAPIError as error:
            raise normalize_telegram_error(error) from error
        return message.message_id


def normalize_telegram_error(error: TelegramAPIError) -> TelegramSendError:
    if isinstance(error, TelegramForbiddenError):
        return TelegramSendError("telegram_forbidden", blocked=True)
    if isinstance(error, TelegramRetryAfter):
        return TelegramSendError(
            "telegram_retry_after",
            transient=True,
            retry_after=timedelta(seconds=error.retry_after),
        )
    if isinstance(error, (TelegramNetworkError, TelegramServerError)):
        return TelegramSendError("telegram_transient", transient=True)
    return TelegramSendError("telegram_permanent")
