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
    CatalogPeriod,
    CatalogSection,
    CatalogState,
    EventLanguage,
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
    CatalogResetCallback,
    CatalogSectionCallback,
    EventAction,
    EventCallback,
    NavigationCallback,
    NavigationView,
    SubscriptionCategoryCallback,
)
from terricon_events_bot.telegram.feedback import FeedbackTelegramController
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
    feedback: FeedbackTelegramController | None = None,
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
            feedback,
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
    state_store.load.return_value = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))
    callback = FakeCallback()
    user = current_user()

    await handler.select_category(
        cast(CallbackQuery, callback),
        CatalogCategoryCallback(category=CategorySlug.DEVELOPMENT_IT),
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
    handler, _, catalog, state_store, renderer = controller(tmp_path)
    state_store.load.return_value = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))
    callback = FakeCallback()

    await handler.select_category(
        cast(CallbackQuery, callback),
        CatalogCategoryCallback(category=CategorySlug.AI_DATA),
        current_user(onboarding=False),
    )

    catalog.list.assert_not_awaited()
    assert renderer.render.await_args.args[2].text.startswith("Выберите язык интерфейса.")


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


def narrowed_state(section: CatalogSection = CatalogSection.UPCOMING) -> CatalogState:
    return CatalogState(
        CatalogFilter(
            section,
            CatalogPeriod.DAYS_30,
            category=CategorySlug.AI_DATA,
            event_format=EventFormat.ONLINE,
            language=EventLanguage.EN,
        ),
        page=3,
    )


@pytest.mark.asyncio
async def test_events_menu_opens_saved_list_on_first_page_in_one_tap(tmp_path: Path) -> None:
    handler, _, catalog, state_store, renderer = controller(tmp_path)
    state_store.load.return_value = narrowed_state()
    catalog.list.return_value = page()

    await handler.navigate(
        cast(CallbackQuery, FakeCallback()),
        NavigationCallback(view=NavigationView.EVENTS),
        current_user(),
    )

    filters, _, requested_page = catalog.list.await_args.args
    assert filters == narrowed_state().filters
    assert requested_page == 1
    text = renderer.render.await_args.args[2].text
    assert "🗓 Актуальные" in text
    assert "AI и Data" in text
    assert "Event" in text


@pytest.mark.asyncio
async def test_category_change_keeps_other_filters_and_section_switch_resets_period(
    tmp_path: Path,
) -> None:
    handler, _, catalog, state_store, _ = controller(tmp_path)
    state_store.load.return_value = narrowed_state()
    catalog.list.return_value = page()

    await handler.select_category(
        cast(CallbackQuery, FakeCallback()),
        CatalogCategoryCallback(category=CategorySlug.OTHER),
        current_user(),
    )
    by_category = state_store.save.await_args.args[1]
    await handler.select_section(
        cast(CallbackQuery, FakeCallback()),
        CatalogSectionCallback(section=CatalogSection.ARCHIVE),
        current_user(),
    )
    by_section = state_store.save.await_args.args[1]

    assert by_category.filters == CatalogFilter(
        CatalogSection.UPCOMING,
        CatalogPeriod.DAYS_30,
        category=CategorySlug.OTHER,
        event_format=EventFormat.ONLINE,
        language=EventLanguage.EN,
    )
    assert by_category.page == 1
    assert by_section.filters == CatalogFilter(
        CatalogSection.ARCHIVE,
        CatalogPeriod.ALL,
        category=CategorySlug.AI_DATA,
        event_format=EventFormat.ONLINE,
        language=EventLanguage.EN,
    )


@pytest.mark.asyncio
async def test_reset_filters_keeps_category_and_returns_to_the_calling_screen(
    tmp_path: Path,
) -> None:
    handler, _, catalog, state_store, renderer = controller(tmp_path)
    state_store.load.return_value = narrowed_state()

    await handler.reset_filters(
        cast(CallbackQuery, FakeCallback()),
        CatalogResetCallback(view=NavigationView.FILTERS),
        current_user(),
    )

    saved = state_store.save.await_args.args[1]
    assert saved.filters == CatalogFilter(
        CatalogSection.UPCOMING, CatalogPeriod.ALL, category=CategorySlug.AI_DATA
    )
    assert saved.page == 1
    catalog.list.assert_not_awaited()
    assert renderer.render.await_args.args[2].text.startswith("<b>Фильтры</b>")


@pytest.mark.asyncio
async def test_start_leaves_feedback_wizard_and_shows_a_fresh_menu(tmp_path: Path) -> None:
    feedback = Mock(spec=FeedbackTelegramController)
    feedback.cancel_draft = AsyncMock()
    handler, _, _, _, renderer = controller(tmp_path, cast(FeedbackTelegramController, feedback))
    user = current_user()

    await handler.start(cast(Any, SimpleNamespace(chat=SimpleNamespace(id=10))), user)

    feedback.cancel_draft.assert_awaited_once_with(user)
    call = renderer.render.await_args
    assert call.kwargs == {"fresh": True}
    assert call.args[2].text.startswith("<b>Главное меню</b>")
