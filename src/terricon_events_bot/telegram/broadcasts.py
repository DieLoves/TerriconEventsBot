from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from terricon_events_bot.telegram.delivery import normalize_telegram_error


class AiogramBroadcastGateway:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(self, telegram_id: int, text: str) -> int:
        try:
            message = await self._bot.send_message(chat_id=telegram_id, text=text)
        except TelegramAPIError as error:
            raise normalize_telegram_error(error) from error
        return message.message_id

    async def send_photo(self, telegram_id: int, telegram_file_id: str) -> int:
        try:
            message = await self._bot.send_photo(
                chat_id=telegram_id,
                photo=telegram_file_id,
            )
        except TelegramAPIError as error:
            raise normalize_telegram_error(error) from error
        return message.message_id

    async def send_report(self, admin_telegram_id: int, text: str) -> int:
        return await self.send_message(admin_telegram_id, text)
