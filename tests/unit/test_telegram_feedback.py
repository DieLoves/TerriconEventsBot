from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot

from terricon_events_bot.application.feedback import (
    FeedbackDraft,
    FeedbackError,
    FeedbackNotification,
    FeedbackService,
)
from terricon_events_bot.domain.enums import FeedbackKind, Locale
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.feedback import (
    AiogramFeedbackGateway,
    FeedbackTelegramController,
)
from terricon_events_bot.telegram.rendering import ScreenRenderer
from terricon_events_bot.telegram.views import TelegramViews

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_feedback_gateway_copies_content_without_forwarding_or_html() -> None:
    bot = Mock(spec=Bot)
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=40))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=41))
    localizations = LocalizationCatalog.load(PROJECT_ROOT / "locales")
    gateway = AiogramFeedbackGateway(cast(Any, bot), -100500, localizations)
    notification = FeedbackNotification(
        ticket_id=7,
        user_id=1,
        telegram_id=100,
        locale=Locale.RU,
        kind=FeedbackKind.ERROR,
        display_name="<Admin>",
        username="member",
        body="x" * 4000,
        telegram_file_id="photo-id",
    )

    result = await gateway.publish(notification)

    assert result == (-100500, 41)
    bot.send_photo.assert_awaited_once_with(chat_id=-100500, photo="photo-id")
    call = bot.send_message.await_args
    assert call.kwargs["chat_id"] == -100500
    assert len(call.kwargs["text"]) <= 4096
    assert "parse_mode" not in call.kwargs
    assert "<Admin>" in call.kwargs["text"]
    assert not hasattr(bot, "forward_message") or bot.forward_message.call_count == 0

    await gateway.send_user_reply(100, Locale.KZ, "Reply body")
    await gateway.notify_closed(100, Locale.KZ)

    assert bot.send_message.await_args_list[-2].kwargs["text"] == (
        "Әкімшінің жауабы:\n\nReply body\n\nЖауап беру үшін осы чатқа хабарлама жазыңыз."  # noqa: RUF001
    )
    assert bot.send_message.await_args_list[-1].kwargs["text"] == "Өтініш жабылды."


def feedback_controller(
    draft: FeedbackDraft | None,
) -> tuple[FeedbackTelegramController, Mock, Mock]:
    from zoneinfo import ZoneInfo

    from terricon_events_bot.infrastructure.assets import AssetResolver

    service = Mock(spec=FeedbackService)
    service.draft = AsyncMock(return_value=draft)
    service.cancel = AsyncMock()
    service.set_text = AsyncMock(side_effect=FeedbackError("Feedback text is too long"))
    service.add_user_reply = AsyncMock()
    renderer = Mock(spec=ScreenRenderer)
    renderer.render = AsyncMock(return_value=1)
    views = TelegramViews(
        LocalizationCatalog.load(PROJECT_ROOT / "locales"),
        AssetResolver(PROJECT_ROOT / "missing-assets"),
        timezone=ZoneInfo("Asia/Almaty"),
    )
    controller = FeedbackTelegramController(
        cast(FeedbackService, service),
        cast(Any, Mock(spec=AiogramFeedbackGateway)),
        cast(Any, Mock()),
        cast(ScreenRenderer, renderer),
        views,
    )
    return controller, service, renderer


def text_message(text: str) -> Any:
    return SimpleNamespace(text=text, photo=None, caption=None, chat=SimpleNamespace(id=100))


@pytest.mark.asyncio
async def test_command_during_feedback_draft_cancels_it_instead_of_becoming_text() -> None:
    controller, service, renderer = feedback_controller(
        FeedbackDraft(FeedbackKind.IDEA, None, None, "text")
    )
    user = User(id=1, telegram_id=100, locale=Locale.RU)

    handled = await controller.handle_message(text_message("/help"), user)

    assert handled is False
    service.cancel.assert_awaited_once_with(1)
    service.set_text.assert_not_awaited()
    service.add_user_reply.assert_not_awaited()
    renderer.render.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_feedback_text_is_reported_to_the_user() -> None:
    controller, service, renderer = feedback_controller(
        FeedbackDraft(FeedbackKind.IDEA, None, None, "text")
    )
    user = User(id=1, telegram_id=100, locale=Locale.RU)

    handled = await controller.handle_message(text_message("x" * 4050), user)

    assert handled is True
    service.set_text.assert_awaited_once()
    screen = renderer.render.await_args.args[2]
    assert "4000" in screen.text
    # The prompt follows the user's message instead of editing a screen above it.
    assert renderer.render.await_args.kwargs == {"fresh": True}
