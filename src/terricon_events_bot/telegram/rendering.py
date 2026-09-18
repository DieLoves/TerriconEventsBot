from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakValueDictionary

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from terricon_events_bot.infrastructure.models import User

_TEXT_LIMIT = 4096
_CAPTION_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class Screen:
    text: str
    keyboard: InlineKeyboardMarkup
    image: str | Path | None = None

    def __post_init__(self) -> None:
        if not self.text or len(self.text) > _TEXT_LIMIT:
            raise ValueError("Screen text must fit a Telegram message")


class ScreenRenderer:
    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._bot = bot
        self._session_factory = session_factory
        self._user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    async def render(self, user_id: int, chat_id: int, screen: Screen) -> int:
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        async with lock:
            return await self._render_serialized(user_id, chat_id, screen)

    async def _render_serialized(self, user_id: int, chat_id: int, screen: Screen) -> int:
        new_message_id: int | None = None
        old_message: tuple[int, int] | None = None
        async with self._session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise LookupError("User does not exist")
            old_chat_id = user.current_menu_chat_id
            old_message_id = user.current_menu_message_id
        image = screen.image if len(screen.text) <= _CAPTION_LIMIT else None

        if old_chat_id == chat_id and old_message_id is not None:
            edited = await self._try_edit(chat_id, old_message_id, screen, image)
            if edited:
                return old_message_id

        try:
            new_message_id = await self._send(chat_id, screen, image)
            async with self._session_factory() as session:
                async with session.begin():
                    user = await session.get(User, user_id, with_for_update=True)
                    if user is None:
                        raise LookupError("User does not exist")
                    user.current_menu_chat_id = chat_id
                    user.current_menu_message_id = new_message_id
                    if old_chat_id is not None and old_message_id is not None:
                        old_message = (old_chat_id, old_message_id)
        except Exception:
            if new_message_id is not None:
                await self._try_delete(chat_id, new_message_id)
            raise

        if old_message is not None:
            await self._try_delete(*old_message)
        assert new_message_id is not None
        return new_message_id

    async def _try_edit(
        self,
        chat_id: int,
        message_id: int,
        screen: Screen,
        image: str | Path | None,
    ) -> bool:
        try:
            if image is None:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=screen.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=screen.keyboard,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            else:
                await self._bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    media=InputMediaPhoto(
                        media=self._media_value(image),
                        caption=screen.text,
                        parse_mode=ParseMode.HTML,
                    ),
                    reply_markup=screen.keyboard,
                )
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.lower():
                return True
            return False
        return True

    async def _send(
        self,
        chat_id: int,
        screen: Screen,
        image: str | Path | None,
    ) -> int:
        if image is not None:
            try:
                message = await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=self._media_value(image),
                    caption=screen.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=screen.keyboard,
                )
                return message.message_id
            except TelegramAPIError:
                pass
        message = await self._bot.send_message(
            chat_id=chat_id,
            text=screen.text,
            parse_mode=ParseMode.HTML,
            reply_markup=screen.keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return message.message_id

    async def _try_delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            return

    @staticmethod
    def _media_value(image: str | Path) -> str | FSInputFile:
        return FSInputFile(image) if isinstance(image, Path) else image
