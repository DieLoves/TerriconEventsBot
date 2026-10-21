from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Update
from test_telegram_flow import FakeTelegramSession, message_payload

from terricon_events_bot.application.broadcasts import BroadcastDraft, BroadcastService
from terricon_events_bot.application.catalog import CatalogQuery, CatalogStateStore
from terricon_events_bot.application.feedback import FeedbackService
from terricon_events_bot.application.subscriptions import SubscriptionService
from terricon_events_bot.application.users import UserContext, UserService
from terricon_events_bot.domain.enums import BroadcastAudience, BroadcastStatus, Locale
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.admin import AdminTelegramController, build_admin_router
from terricon_events_bot.telegram.feedback import AiogramFeedbackGateway
from terricon_events_bot.telegram.middleware import UserContextMiddleware
from terricon_events_bot.telegram.rendering import ScreenRenderer
from terricon_events_bot.telegram.router import build_user_router
from terricon_events_bot.telegram.views import TelegramViews

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_application(
    tmp_path: Path,
    *,
    feedback: Mock,
    broadcasts: Mock,
    gateway: Mock,
) -> tuple[Dispatcher, Mock, Mock]:
    current_user = User(id=1, telegram_id=100, locale=Locale.RU, onboarding_completed=True)
    users = Mock(spec=UserService)
    users.resolve = AsyncMock(return_value=UserContext(current_user, is_admin=True))
    renderer = Mock(spec=ScreenRenderer)
    renderer.render = AsyncMock(return_value=10)
    localizations = LocalizationCatalog.load(PROJECT_ROOT / "locales")
    views = TelegramViews(localizations, AssetResolver(tmp_path), timezone=ZoneInfo("Asia/Almaty"))
    middleware = UserContextMiddleware(cast(UserService, users), localizations)
    root = Router(name="application-test")
    root.message.outer_middleware(middleware)
    root.callback_query.outer_middleware(middleware)
    root.include_router(
        build_admin_router(
            AdminTelegramController(
                cast(Any, Mock()),
                cast(FeedbackService, feedback),
                cast(BroadcastService, broadcasts),
                cast(Any, Mock()),
                cast(AiogramFeedbackGateway, gateway),
                cast(UserService, users),
            )
        )
    )
    root.include_router(
        build_user_router(
            cast(UserService, users),
            cast(SubscriptionService, Mock(spec=SubscriptionService)),
            cast(CatalogQuery, Mock(spec=CatalogQuery)),
            cast(CatalogStateStore, Mock(spec=CatalogStateStore)),
            cast(ScreenRenderer, renderer),
            views,
        )
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(root)
    return dispatcher, users, renderer


async def feed_text(dispatcher: Dispatcher, text: str) -> FakeTelegramSession:
    session = FakeTelegramSession()
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE12345678", session=session)
    try:
        async with asyncio.timeout(5):
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    {"update_id": 1, "message": message_payload(text=text)},
                    context={"bot": bot},
                ),
            )
    finally:
        await dispatcher.storage.close()
        await dispatcher.fsm.events_isolation.close()
        await bot.session.close()
    return session


def admin_mocks(
    *, pending_ticket: int | None, draft: BroadcastDraft | None
) -> tuple[Mock, Mock, Mock]:
    feedback = Mock(spec=FeedbackService)
    feedback.pending_admin_reply = AsyncMock(return_value=pending_ticket)
    feedback.cancel_admin_reply = AsyncMock(return_value=pending_ticket is not None)
    broadcasts = Mock(spec=BroadcastService)
    broadcasts.active_draft = AsyncMock(return_value=draft)
    broadcasts.cancel = AsyncMock()
    broadcasts.set_body = AsyncMock()
    gateway = Mock(spec=AiogramFeedbackGateway)
    gateway.send_user_reply = AsyncMock(return_value=1)
    return feedback, broadcasts, gateway


@pytest.mark.asyncio
async def test_admin_command_is_not_sent_as_pending_ticket_reply(tmp_path: Path) -> None:
    feedback, broadcasts, gateway = admin_mocks(pending_ticket=7, draft=None)
    dispatcher, users, renderer = build_application(
        tmp_path, feedback=feedback, broadcasts=broadcasts, gateway=gateway
    )

    await feed_text(dispatcher, "/start")

    gateway.send_user_reply.assert_not_awaited()
    renderer.render.assert_awaited_once()
    users.resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_command_is_not_used_as_broadcast_body(tmp_path: Path) -> None:
    draft = BroadcastDraft(
        broadcast_id=3,
        created_by_telegram_id=100,
        audience=BroadcastAudience.RU,
        bodies={},
        telegram_file_id=None,
        status=BroadcastStatus.DRAFT,
    )
    feedback, broadcasts, gateway = admin_mocks(pending_ticket=None, draft=draft)
    dispatcher, _, renderer = build_application(
        tmp_path, feedback=feedback, broadcasts=broadcasts, gateway=gateway
    )

    await feed_text(dispatcher, "/start")

    broadcasts.set_body.assert_not_awaited()
    renderer.render.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_cancel_drops_pending_reply_and_broadcast_draft(tmp_path: Path) -> None:
    draft = BroadcastDraft(
        broadcast_id=3,
        created_by_telegram_id=100,
        audience=BroadcastAudience.RU,
        bodies={},
        telegram_file_id=None,
        status=BroadcastStatus.DRAFT,
    )
    feedback, broadcasts, gateway = admin_mocks(pending_ticket=7, draft=draft)
    dispatcher, _, renderer = build_application(
        tmp_path, feedback=feedback, broadcasts=broadcasts, gateway=gateway
    )

    session = await feed_text(dispatcher, "/cancel")

    feedback.cancel_admin_reply.assert_awaited_once_with(1)
    broadcasts.cancel.assert_awaited_once_with(3)
    gateway.send_user_reply.assert_not_awaited()
    renderer.render.assert_not_awaited()
    assert len(session.methods) == 1
