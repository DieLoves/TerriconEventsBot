from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from terricon_events_bot.telegram.delivery import normalize_telegram_error


class AiogramAdminAlertGateway:
    def __init__(self, bot: Bot, admin_chat_id: int) -> None:
        self._bot = bot
        self._admin_chat_id = admin_chat_id

    async def send_alert(self, text_value: str) -> int:
        try:
            message = await self._bot.send_message(
                chat_id=self._admin_chat_id,
                text=text_value,
            )
        except TelegramAPIError as error:
            raise normalize_telegram_error(error) from error
        return message.message_id
