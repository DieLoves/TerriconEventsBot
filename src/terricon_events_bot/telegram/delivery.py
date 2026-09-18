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
        except TelegramForbiddenError as error:
            raise TelegramSendError("telegram_forbidden", blocked=True) from error
        except TelegramRetryAfter as error:
            raise TelegramSendError(
                "telegram_retry_after",
                transient=True,
                retry_after=timedelta(seconds=error.retry_after),
            ) from error
        except (TelegramNetworkError, TelegramServerError) as error:
            raise TelegramSendError("telegram_transient", transient=True) from error
        except TelegramAPIError as error:
            raise TelegramSendError("telegram_permanent") from error
        return message.message_id
