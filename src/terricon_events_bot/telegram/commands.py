from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from terricon_events_bot.domain.enums import Locale
from terricon_events_bot.localization import LocalizationCatalog

_LOGGER = logging.getLogger(__name__)
# Telegram identifies Kazakh by its IETF code; RU is the default command list.
_TELEGRAM_LANGUAGE_CODES: dict[Locale, str | None] = {Locale.RU: None, Locale.KZ: "kk"}


async def publish_user_commands(bot: Bot, localizations: LocalizationCatalog) -> None:
    """Expose /start in the Telegram command menu; failures never block startup."""
    for locale, language_code in _TELEGRAM_LANGUAGE_CODES.items():
        description = localizations.get(locale, "commands.start")
        try:
            await bot.set_my_commands(
                [BotCommand(command="start", description=description)],
                scope=BotCommandScopeAllPrivateChats(),
                language_code=language_code,
            )
        except TelegramAPIError:
            _LOGGER.warning("Bot command menu was not published", extra={"locale": locale.value})
