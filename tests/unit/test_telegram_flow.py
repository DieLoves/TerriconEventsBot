from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, TypeVar, cast
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Update

from terricon_events_bot.application.catalog import CatalogQuery, CatalogStateStore
from terricon_events_bot.application.subscriptions import SubscriptionService
from terricon_events_bot.application.users import UserContext, UserService
from terricon_events_bot.domain.enums import Locale
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.admin import AdminTelegramController, build_admin_router
from terricon_events_bot.telegram.callbacks import ChooseLocaleCallback
from terricon_events_bot.telegram.middleware import UserContextMiddleware
from terricon_events_bot.telegram.rendering import ScreenRenderer
from terricon_events_bot.telegram.router import build_user_router
from terricon_events_bot.telegram.views import TelegramViews

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TelegramResult = TypeVar("TelegramResult")


class FakeTelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramResult],
        timeout: int | None = None,  # noqa: ASYNC109 - framework override
    ) -> TelegramResult:
        self.methods.append(method)
        return cast(TelegramResult, True)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - framework override
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        if False:  # pragma: no cover - satisfies the async-generator protocol
            yield b""


def telegram_user() -> dict[str, object]:
    return {"id": 100, "is_bot": False, "first_name": "User", "username": "member"}


def message_payload(*, text: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "message_id": 10,
        "date": 1_700_000_000,
        "chat": {"id": 100, "type": "private"},
        "from": telegram_user(),
    }
    if text is not None:
        payload["text"] = text
        payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    return payload


@pytest.mark.asyncio
async def test_start_and_locale_callback_flow_through_router_middleware(tmp_path: Path) -> None:
    current_user = User(id=1, telegram_id=100, locale=Locale.RU, onboarding_completed=False)
    users = Mock(spec=UserService)
    users.resolve = AsyncMock(return_value=UserContext(current_user, is_admin=False))
    users.set_locale = AsyncMock()
    subscriptions = Mock(spec=SubscriptionService)
    catalog = Mock(spec=CatalogQuery)
    state_store = Mock(spec=CatalogStateStore)
    renderer = Mock(spec=ScreenRenderer)
    renderer.render = AsyncMock(return_value=10)
    localizations = LocalizationCatalog.load(PROJECT_ROOT / "locales")
    views = TelegramViews(
        localizations,
        AssetResolver(tmp_path),
        timezone=ZoneInfo("Asia/Almaty"),
    )
    router = build_user_router(
        cast(UserService, users),
        cast(SubscriptionService, subscriptions),
        cast(CatalogQuery, catalog),
        cast(CatalogStateStore, state_store),
        cast(ScreenRenderer, renderer),
        views,
        UserContextMiddleware(cast(UserService, users), localizations),
    )
    middleware = UserContextMiddleware(cast(UserService, users), localizations)
    admin_controller = AdminTelegramController(
        cast(Any, Mock()),
        cast(Any, Mock()),
        cast(Any, Mock()),
        cast(Any, Mock()),
        cast(Any, Mock()),
        cast(UserService, users),
    )
    root = Router(name="application-test")
    root.include_router(build_admin_router(admin_controller, middleware))
    root.include_router(router)
    dispatcher = Dispatcher()
    dispatcher.include_router(root)
    session = FakeTelegramSession()
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE12345678", session=session)

    try:
        async with asyncio.timeout(5):
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    {"update_id": 1, "message": message_payload(text="/start")},
                    context={"bot": bot},
                ),
            )
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    {
                        "update_id": 2,
                        "callback_query": {
                            "id": "callback-1",
                            "from": telegram_user(),
                            "chat_instance": "instance",
                            "data": ChooseLocaleCallback(locale=Locale.KZ).pack(),
                            "message": message_payload(),
                        },
                    },
                    context={"bot": bot},
                ),
            )
    finally:
        await dispatcher.storage.close()
        await dispatcher.fsm.events_isolation.close()
        await bot.session.close()

    assert renderer.render.await_count == 2
    assert renderer.render.await_args_list[0].args[2].text == "Выберите язык интерфейса."
    assert "Terricon Events Bot" in renderer.render.await_args_list[1].args[2].text
    users.set_locale.assert_awaited_once_with(1, Locale.KZ)
    assert current_user.locale is Locale.KZ
    assert len(session.methods) == 1
