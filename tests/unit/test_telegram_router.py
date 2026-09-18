from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery

from terricon_events_bot.application.catalog import CatalogQuery, CatalogStateStore
from terricon_events_bot.application.subscriptions import (
    SubscriptionService,
    SubscriptionSummary,
)
from terricon_events_bot.application.users import UserService
from terricon_events_bot.domain.catalog import (
    CatalogFilter,
    CatalogPage,
    CatalogSection,
    CatalogState,
    EventSummary,
)
from terricon_events_bot.domain.enums import (
    CategorySlug,
    EventFormat,
    Locale,
    TranslationSource,
)
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.callbacks import (
    CatalogCategoryCallback,
    EventAction,
    EventCallback,
    SubscriptionCategoryCallback,
)
from terricon_events_bot.telegram.rendering import ScreenRenderer
from terricon_events_bot.telegram.router import TelegramController
from terricon_events_bot.telegram.views import TelegramViews

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeCallback:
    def __init__(self) -> None:
        self.bot = object()
        self.message = SimpleNamespace(chat=SimpleNamespace(id=10))
        self.answer = AsyncMock()


def controller(
    tmp_path: Path,
) -> tuple[TelegramController, Any, Any, Any, Any]:
    users = Mock(spec=UserService)
    users.set_locale = AsyncMock()
    users.complete_onboarding = AsyncMock()
    users.toggle_menu_images = AsyncMock(return_value=False)
    users.toggle_event_posters = AsyncMock(return_value=False)
    subscriptions = Mock(spec=SubscriptionService)
    subscriptions.get = AsyncMock()
    subscriptions.toggle_all = AsyncMock()
    subscriptions.toggle_category = AsyncMock()
    catalog = Mock(spec=CatalogQuery)
    catalog.list = AsyncMock()
    catalog.get = AsyncMock()
    state_store = Mock(spec=CatalogStateStore)
    state_store.load = AsyncMock()
    state_store.save = AsyncMock()
    renderer = Mock(spec=ScreenRenderer)
    renderer.render = AsyncMock()
    views = TelegramViews(
        LocalizationCatalog.load(PROJECT_ROOT / "locales"),
        AssetResolver(tmp_path),
        timezone=ZoneInfo("Asia/Almaty"),
    )
    return (
        TelegramController(
            cast(UserService, users),
            cast(SubscriptionService, subscriptions),
            cast(CatalogQuery, catalog),
            cast(CatalogStateStore, state_store),
            cast(ScreenRenderer, renderer),
            views,
        ),
        subscriptions,
        catalog,
        state_store,
        renderer,
    )


def current_user(*, onboarding: bool = True) -> User:
    return User(
        id=1,
        telegram_id=100,
        locale=Locale.RU,
        onboarding_completed=onboarding,
    )


def page(event_id: int = 1) -> CatalogPage:
    return CatalogPage(
        items=(
            EventSummary(
                id=event_id,
                title="Event",
                starts_at=datetime(2030, 1, 1, tzinfo=UTC),
                event_format=EventFormat.OFFLINE,
                categories=(CategorySlug.DEVELOPMENT_IT,),
                translation_source=TranslationSource.OFFICIAL,
            ),
        ),
        page=1,
        pages=1,
        total=1,
        stale=False,
    )


@pytest.mark.asyncio
async def test_category_callback_persists_state_and_renders_results(tmp_path: Path) -> None:
    handler, _, catalog, state_store, renderer = controller(tmp_path)
    catalog.list.return_value = page()
    callback = FakeCallback()
    user = current_user()

    await handler.select_category(
        cast(CallbackQuery, callback),
        CatalogCategoryCallback(
            section=CatalogSection.UPCOMING,
            category=CategorySlug.DEVELOPMENT_IT,
        ),
        user,
    )

    callback.answer.assert_awaited_once()
    saved = state_store.save.await_args.args[1]
    assert saved.filters.category is CategorySlug.DEVELOPMENT_IT
    assert saved.page == 1
    renderer.render.assert_awaited_once()
    assert "Event" in renderer.render.await_args.args[2].text


@pytest.mark.asyncio
async def test_tampered_event_callback_cannot_open_event_outside_current_page(
    tmp_path: Path,
) -> None:
    handler, _, catalog, state_store, renderer = controller(tmp_path)
    state_store.load.return_value = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))
    catalog.list.return_value = page(event_id=1)
    callback = FakeCallback()

    await handler.show_event(
        cast(CallbackQuery, callback),
        EventCallback(event_id=999, action=EventAction.DESCRIPTION),
        current_user(),
    )

    catalog.get.assert_not_awaited()
    assert "Действие устарело" in renderer.render.await_args.args[2].text


@pytest.mark.asyncio
async def test_onboarding_guard_rejects_stale_catalog_callbacks(tmp_path: Path) -> None:
    handler, _, catalog, _, renderer = controller(tmp_path)
    callback = FakeCallback()

    await handler.select_category(
        cast(CallbackQuery, callback),
        CatalogCategoryCallback(
            section=CatalogSection.UPCOMING,
            category=CategorySlug.AI_DATA,
        ),
        current_user(onboarding=False),
    )

    catalog.list.assert_not_awaited()
    assert renderer.render.await_args.args[2].text == "Выберите язык интерфейса."


@pytest.mark.asyncio
async def test_subscription_category_callback_persists_and_renders_selection(
    tmp_path: Path,
) -> None:
    handler, subscriptions, _, _, renderer = controller(tmp_path)
    subscriptions.toggle_category.return_value = SubscriptionSummary(
        subscribe_all=False,
        categories=(CategorySlug.AI_DATA,),
    )
    callback = FakeCallback()

    await handler.toggle_subscription_category(
        cast(CallbackQuery, callback),
        SubscriptionCategoryCallback(category=CategorySlug.AI_DATA),
        current_user(),
    )

    subscriptions.toggle_category.assert_awaited_once_with(1, CategorySlug.AI_DATA)
    assert "AI и Data" in renderer.render.await_args.args[2].text
