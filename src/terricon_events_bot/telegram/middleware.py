from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from terricon_events_bot.application.users import TelegramPrincipal, UserService
from terricon_events_bot.domain.enums import Locale
from terricon_events_bot.localization import LocalizationCatalog


class UserContextMiddleware(BaseMiddleware):
    def __init__(self, users: UserService, localizations: LocalizationCatalog) -> None:
        self._users = users
        self._localizations = localizations

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user = getattr(event, "from_user", None)
        if telegram_user is None:
            return await handler(event, data)
        context = await self._users.resolve(
            TelegramPrincipal(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                display_name=telegram_user.full_name,
            )
        )
        if context is None:
            await self._deny(event)
            return None
        data["current_user"] = context.user
        data["user_context"] = context
        data["is_admin"] = context.is_admin
        return await handler(event, data)

    async def _deny(self, event: TelegramObject) -> None:
        text = self._localizations.get(Locale.RU, "errors.forbidden")
        if isinstance(event, CallbackQuery):
            await event.answer(text=text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
