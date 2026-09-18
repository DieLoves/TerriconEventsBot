from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendPhoto
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.telegram.rendering import Screen, ScreenRenderer

pytestmark = pytest.mark.postgres


class FakeBot:
    def __init__(
        self,
        *,
        edit_error: TelegramBadRequest | None = None,
        photo_error: TelegramBadRequest | None = None,
    ) -> None:
        self.edit_error = edit_error
        self.photo_error = photo_error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_message_id = 20

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.calls.append(("edit_text", kwargs))
        if self.edit_error is not None:
            raise self.edit_error

    async def edit_message_media(self, **kwargs: Any) -> None:
        self.calls.append(("edit_media", kwargs))
        if self.edit_error is not None:
            raise self.edit_error

    async def send_photo(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("send_photo", kwargs))
        if self.photo_error is not None:
            raise self.photo_error
        return SimpleNamespace(message_id=self.next_message_id)

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("send_message", kwargs))
        return SimpleNamespace(message_id=self.next_message_id)

    async def delete_message(self, **kwargs: Any) -> None:
        self.calls.append(("delete", kwargs))


@pytest.fixture
async def renderer_database(
    migrated_database_url: str,
) -> Iterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(migrated_database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
    try:
        yield engine, create_session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
        await engine.dispose()


def bad_request(message: str, *, photo: bool = False) -> TelegramBadRequest:
    method = SendPhoto(chat_id=1, photo="broken") if photo else EditMessageText(text="text")
    return TelegramBadRequest(method, message)


def screen(*, image: str | None = None) -> Screen:
    return Screen(text="Screen", keyboard=InlineKeyboardMarkup(inline_keyboard=[]), image=image)


async def add_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    current_message: int | None,
) -> int:
    async with session_factory() as session:
        async with session.begin():
            user = User(
                telegram_id=100,
                current_menu_chat_id=10 if current_message is not None else None,
                current_menu_message_id=current_message,
            )
            session.add(user)
            await session.flush()
            return user.id


@pytest.mark.asyncio
async def test_deleted_or_incompatible_screen_is_replaced_and_persisted(
    renderer_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = renderer_database
    user_id = await add_user(session_factory, current_message=11)
    bot = FakeBot(edit_error=bad_request("message to edit not found"))
    renderer = ScreenRenderer(cast(Bot, bot), session_factory)

    message_id = await renderer.render(user_id, 10, screen())

    async with session_factory() as session:
        stored = await session.get(User, user_id)
    assert message_id == 20
    assert stored is not None and stored.current_menu_message_id == 20
    assert [name for name, _ in bot.calls] == ["edit_text", "send_message", "delete"]
    assert bot.calls[-1][1] == {"chat_id": 10, "message_id": 11}


@pytest.mark.asyncio
async def test_not_modified_is_success_without_duplicate_message(
    renderer_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = renderer_database
    user_id = await add_user(session_factory, current_message=11)
    bot = FakeBot(edit_error=bad_request("Bad Request: message is not modified"))
    renderer = ScreenRenderer(cast(Bot, bot), session_factory)

    assert await renderer.render(user_id, 10, screen()) == 11
    assert [name for name, _ in bot.calls] == ["edit_text"]


@pytest.mark.asyncio
async def test_invalid_remote_photo_falls_back_to_text_and_tracks_screen(
    renderer_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, session_factory = renderer_database
    user_id = await add_user(session_factory, current_message=None)
    bot = FakeBot(photo_error=bad_request("wrong file identifier", photo=True))
    renderer = ScreenRenderer(cast(Bot, bot), session_factory)

    assert await renderer.render(user_id, 10, screen(image="https://example.test/poster")) == 20

    async with session_factory() as session:
        stored = await session.get(User, user_id)
    assert stored is not None
    assert (stored.current_menu_chat_id, stored.current_menu_message_id) == (10, 20)
    assert [name for name, _ in bot.calls] == ["send_photo", "send_message"]
