from __future__ import annotations

from dataclasses import replace

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from terricon_events_bot.application.catalog import CatalogQuery, CatalogStateStore
from terricon_events_bot.application.subscriptions import SubscriptionService
from terricon_events_bot.application.users import UserService
from terricon_events_bot.domain.catalog import CatalogFilter, CatalogPage, CatalogState
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.telegram.callbacks import (
    CatalogAllCallback,
    CatalogCategoryCallback,
    CatalogFormatAllCallback,
    CatalogFormatCallback,
    CatalogLanguageAllCallback,
    CatalogLanguageCallback,
    CatalogPageCallback,
    CatalogPeriodCallback,
    CatalogSectionCallback,
    ChooseLocaleCallback,
    ContinueOnboardingCallback,
    DeleteProfileCallback,
    EventAction,
    EventCallback,
    FeedbackActionCallback,
    FeedbackKindCallback,
    NavigationCallback,
    NavigationView,
    SettingLocaleCallback,
    SettingToggleCallback,
    SubscriptionAllCallback,
    SubscriptionCategoryCallback,
    ToggleSetting,
)
from terricon_events_bot.telegram.feedback import FeedbackTelegramController
from terricon_events_bot.telegram.middleware import UserContextMiddleware
from terricon_events_bot.telegram.rendering import Screen, ScreenRenderer
from terricon_events_bot.telegram.views import TelegramViews


class TelegramController:
    def __init__(
        self,
        users: UserService,
        subscriptions: SubscriptionService,
        catalog: CatalogQuery,
        catalog_state: CatalogStateStore,
        renderer: ScreenRenderer,
        views: TelegramViews,
        feedback: FeedbackTelegramController | None = None,
    ) -> None:
        self._users = users
        self._subscriptions = subscriptions
        self._catalog = catalog
        self._catalog_state = catalog_state
        self._renderer = renderer
        self._views = views
        self._feedback = feedback

    async def start(self, message: Message, current_user: User) -> None:
        await self._render_message(
            message,
            current_user,
            self._views.main_menu(current_user)
            if current_user.onboarding_completed
            else self._views.onboarding_language(),
        )

    async def unknown_message(self, message: Message, current_user: User) -> None:
        if self._feedback is not None and await self._feedback.handle_message(
            message, current_user
        ):
            return
        await self.start(message, current_user)

    async def choose_locale(
        self,
        callback: CallbackQuery,
        callback_data: ChooseLocaleCallback,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if current_user.onboarding_completed:
            await self._render_callback(callback, current_user, self._views.main_menu(current_user))
            return
        await self._users.set_locale(current_user.id, callback_data.locale)
        current_user.locale = callback_data.locale
        await self._render_callback(
            callback,
            current_user,
            self._views.onboarding_intro(current_user.locale),
        )

    async def continue_onboarding(
        self,
        callback: CallbackQuery,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not current_user.onboarding_completed:
            await self._users.complete_onboarding(current_user.id)
            current_user.onboarding_completed = True
        await self._render_callback(callback, current_user, self._views.main_menu(current_user))

    async def navigate(
        self,
        callback: CallbackQuery,
        callback_data: NavigationCallback,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, current_user):
            return
        view = callback_data.view
        if view is NavigationView.MENU:
            screen = self._views.main_menu(current_user)
        elif view is NavigationView.EVENTS:
            screen = self._views.event_sections(current_user)
        elif view is NavigationView.FILTERS:
            screen = self._views.filters(
                current_user, await self._catalog_state.load(current_user.id)
            )
        elif view is NavigationView.SETTINGS:
            screen = self._views.settings(current_user)
        elif view is NavigationView.SUBSCRIPTIONS:
            screen = self._views.subscriptions(
                current_user, await self._subscriptions.get(current_user.id)
            )
        elif view is NavigationView.FEEDBACK:
            screen = self._views.feedback(current_user)
        elif view is NavigationView.ABOUT:
            screen = self._views.about(current_user)
        elif view is NavigationView.PRIVACY:
            screen = self._views.privacy(current_user)
        elif view is NavigationView.PRIVACY_WARNING:
            screen = self._views.privacy(current_user, warning=True)
        else:  # pragma: no cover - closed enum makes this defensive
            screen = self._views.invalid_action(current_user)
        await self._render_callback(callback, current_user, screen)

    async def select_section(
        self,
        callback: CallbackQuery,
        callback_data: CatalogSectionCallback,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, current_user):
            return
        state = CatalogState(CatalogFilter.default(callback_data.section))
        await self._catalog_state.save(current_user.id, state)
        await self._render_callback(
            callback,
            current_user,
            self._views.categories(current_user, callback_data.section),
        )

    async def select_category(
        self,
        callback: CallbackQuery,
        callback_data: CatalogCategoryCallback,
        current_user: User,
    ) -> None:
        state = CatalogState(
            replace(
                CatalogFilter.default(callback_data.section),
                category=callback_data.category,
            )
        )
        await self._show_catalog(callback, current_user, state)

    async def select_all_events(
        self,
        callback: CallbackQuery,
        callback_data: CatalogAllCallback,
        current_user: User,
    ) -> None:
        await self._show_catalog(
            callback,
            current_user,
            CatalogState(CatalogFilter.default(callback_data.section)),
        )

    async def select_page(
        self,
        callback: CallbackQuery,
        callback_data: CatalogPageCallback,
        current_user: User,
    ) -> None:
        state = await self._catalog_state.load(current_user.id)
        if callback_data.page < 1:
            await self._show_invalid(callback, current_user)
            return
        await self._show_catalog(callback, current_user, replace(state, page=callback_data.page))

    async def select_period(
        self,
        callback: CallbackQuery,
        callback_data: CatalogPeriodCallback,
        current_user: User,
    ) -> None:
        state = await self._catalog_state.load(current_user.id)
        try:
            changed = state.with_filters(replace(state.filters, period=callback_data.period))
        except ValueError:
            await self._show_invalid(callback, current_user)
            return
        await self._save_and_show_filters(callback, current_user, changed)

    async def select_format(
        self,
        callback: CallbackQuery,
        callback_data: CatalogFormatCallback,
        current_user: User,
    ) -> None:
        state = await self._catalog_state.load(current_user.id)
        try:
            changed = state.with_filters(
                replace(state.filters, event_format=callback_data.event_format)
            )
        except ValueError:
            await self._show_invalid(callback, current_user)
            return
        await self._save_and_show_filters(callback, current_user, changed)

    async def clear_format(self, callback: CallbackQuery, current_user: User) -> None:
        state = await self._catalog_state.load(current_user.id)
        await self._save_and_show_filters(
            callback,
            current_user,
            state.with_filters(replace(state.filters, event_format=None)),
        )

    async def select_language(
        self,
        callback: CallbackQuery,
        callback_data: CatalogLanguageCallback,
        current_user: User,
    ) -> None:
        state = await self._catalog_state.load(current_user.id)
        await self._save_and_show_filters(
            callback,
            current_user,
            state.with_filters(replace(state.filters, language=callback_data.language)),
        )

    async def clear_language(self, callback: CallbackQuery, current_user: User) -> None:
        state = await self._catalog_state.load(current_user.id)
        await self._save_and_show_filters(
            callback,
            current_user,
            state.with_filters(replace(state.filters, language=None)),
        )

    async def show_event(
        self,
        callback: CallbackQuery,
        callback_data: EventCallback,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, current_user):
            return
        state = await self._catalog_state.load(current_user.id)
        located = await self._event_page(
            state,
            current_user,
            callback_data.event_id,
            callback_data.list_page,
        )
        if located is None:
            await self._render_callback(
                callback, current_user, self._views.invalid_action(current_user)
            )
            return
        page, position = located
        event = await self._catalog.get(callback_data.event_id, current_user.locale)
        if event is None:
            await self._render_callback(
                callback, current_user, self._views.invalid_action(current_user)
            )
            return
        if callback_data.action is EventAction.DESCRIPTION:
            screen = self._views.description(
                current_user,
                event,
                list_page=callback_data.list_page,
                content_page=callback_data.content_page,
            )
        else:
            previous, following = await self._adjacent_events(state, current_user, page, position)
            screen = self._views.event_card(
                current_user,
                state,
                event,
                page=callback_data.list_page,
                previous=previous,
                following=following,
            )
        await self._render_callback(callback, current_user, screen)

    async def set_locale(
        self,
        callback: CallbackQuery,
        callback_data: SettingLocaleCallback,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, current_user):
            return
        await self._users.set_locale(current_user.id, callback_data.locale)
        current_user.locale = callback_data.locale
        await self._render_callback(callback, current_user, self._views.settings(current_user))

    async def toggle_setting(
        self,
        callback: CallbackQuery,
        callback_data: SettingToggleCallback,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, current_user):
            return
        if callback_data.setting is ToggleSetting.MENU_IMAGES:
            current_user.menu_images_enabled = await self._users.toggle_menu_images(current_user.id)
        else:
            current_user.event_posters_enabled = await self._users.toggle_event_posters(
                current_user.id
            )
        await self._render_callback(callback, current_user, self._views.settings(current_user))

    async def toggle_subscription_all(
        self,
        callback: CallbackQuery,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, current_user):
            return
        summary = await self._subscriptions.toggle_all(current_user.id)
        await self._render_callback(
            callback, current_user, self._views.subscriptions(current_user, summary)
        )

    async def toggle_subscription_category(
        self,
        callback: CallbackQuery,
        callback_data: SubscriptionCategoryCallback,
        current_user: User,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, current_user):
            return
        summary = await self._subscriptions.toggle_category(current_user.id, callback_data.category)
        await self._render_callback(
            callback, current_user, self._views.subscriptions(current_user, summary)
        )

    async def unknown_callback(self, callback: CallbackQuery, current_user: User) -> None:
        await self._show_invalid(callback, current_user)

    async def _show_catalog(
        self,
        callback: CallbackQuery,
        user: User,
        state: CatalogState,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, user):
            return
        page = await self._catalog.list(state.filters, user.locale, state.page)
        persisted = replace(state, page=page.page)
        await self._catalog_state.save(user.id, persisted)
        await self._render_callback(
            callback,
            user,
            self._views.catalog_list(user, persisted, page),
        )

    async def _save_and_show_filters(
        self,
        callback: CallbackQuery,
        user: User,
        state: CatalogState,
    ) -> None:
        await self._answer(callback)
        if not await self._require_onboarding(callback, user):
            return
        await self._catalog_state.save(user.id, state)
        await self._render_callback(callback, user, self._views.filters(user, state))

    async def _event_page(
        self,
        state: CatalogState,
        user: User,
        event_id: int,
        requested_page: int,
    ) -> tuple[CatalogPage, int] | None:
        if requested_page < 1:
            return None
        page = await self._catalog.list(state.filters, user.locale, requested_page)
        if page.page != requested_page:
            return None
        ids = [item.id for item in page.items]
        try:
            position = ids.index(event_id)
        except ValueError:
            return None
        return page, position

    async def _adjacent_events(
        self,
        state: CatalogState,
        user: User,
        page: CatalogPage,
        position: int,
    ) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        ids = [item.id for item in page.items]
        previous = (ids[position - 1], page.page) if position > 0 else None
        following = (ids[position + 1], page.page) if position + 1 < len(ids) else None
        if previous is None or following is None:
            adjacent = await self._catalog.adjacent_ids(state.filters, ids[position])
            if adjacent is not None:
                previous_id, following_id = adjacent
                if previous is None and previous_id is not None:
                    previous = (previous_id, page.page - 1)
                if following is None and following_id is not None:
                    following = (following_id, page.page + 1)
        return previous, following

    async def _show_invalid(self, callback: CallbackQuery, user: User) -> None:
        await self._answer(callback)
        screen = (
            self._views.invalid_action(user)
            if user.onboarding_completed
            else self._views.onboarding_language()
        )
        await self._render_callback(callback, user, screen)

    async def _require_onboarding(self, callback: CallbackQuery, user: User) -> bool:
        if user.onboarding_completed:
            return True
        await self._render_callback(callback, user, self._views.onboarding_language())
        return False

    async def _render_message(self, message: Message, user: User, screen: Screen) -> None:
        await self._renderer.render(user.id, message.chat.id, screen)

    async def _render_callback(self, callback: CallbackQuery, user: User, screen: Screen) -> None:
        if callback.message is None:
            return
        await self._renderer.render(user.id, callback.message.chat.id, screen)

    @staticmethod
    async def _answer(callback: CallbackQuery) -> None:
        if not callback.bot:
            return
        await callback.answer()


def build_user_router(
    users: UserService,
    subscriptions: SubscriptionService,
    catalog: CatalogQuery,
    catalog_state: CatalogStateStore,
    renderer: ScreenRenderer,
    views: TelegramViews,
    middleware: UserContextMiddleware,
    feedback: FeedbackTelegramController | None = None,
) -> Router:
    controller = TelegramController(
        users,
        subscriptions,
        catalog,
        catalog_state,
        renderer,
        views,
        feedback,
    )
    router = Router(name="user-ui")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)

    router.message.register(controller.start, CommandStart())
    router.callback_query.register(controller.choose_locale, ChooseLocaleCallback.filter())
    router.callback_query.register(
        controller.continue_onboarding, ContinueOnboardingCallback.filter()
    )
    router.callback_query.register(controller.navigate, NavigationCallback.filter())
    router.callback_query.register(controller.select_section, CatalogSectionCallback.filter())
    router.callback_query.register(controller.select_category, CatalogCategoryCallback.filter())
    router.callback_query.register(controller.select_all_events, CatalogAllCallback.filter())
    router.callback_query.register(controller.select_period, CatalogPeriodCallback.filter())
    router.callback_query.register(controller.select_format, CatalogFormatCallback.filter())
    router.callback_query.register(controller.clear_format, CatalogFormatAllCallback.filter())
    router.callback_query.register(controller.select_language, CatalogLanguageCallback.filter())
    router.callback_query.register(controller.clear_language, CatalogLanguageAllCallback.filter())
    router.callback_query.register(controller.select_page, CatalogPageCallback.filter())
    router.callback_query.register(controller.show_event, EventCallback.filter())
    router.callback_query.register(controller.set_locale, SettingLocaleCallback.filter())
    router.callback_query.register(controller.toggle_setting, SettingToggleCallback.filter())
    router.callback_query.register(
        controller.toggle_subscription_all, SubscriptionAllCallback.filter()
    )
    router.callback_query.register(
        controller.toggle_subscription_category, SubscriptionCategoryCallback.filter()
    )
    if feedback is not None:
        router.callback_query.register(feedback.choose_kind, FeedbackKindCallback.filter())
        router.callback_query.register(feedback.action, FeedbackActionCallback.filter())
        router.callback_query.register(feedback.delete_profile, DeleteProfileCallback.filter())
    router.callback_query.register(controller.unknown_callback)
    router.message.register(controller.unknown_message)
    return router
