from __future__ import annotations

import html
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from terricon_events_bot.application.feedback import FeedbackDraft
from terricon_events_bot.application.subscriptions import SubscriptionSummary
from terricon_events_bot.domain.catalog import (
    CatalogFilter,
    CatalogPage,
    CatalogPeriod,
    CatalogScope,
    CatalogSection,
    CatalogState,
    EventDetails,
    EventLanguage,
)
from terricon_events_bot.domain.enums import (
    CategorySlug,
    EventFormat,
    FeedbackKind,
    Locale,
    TranslationSource,
)
from terricon_events_bot.domain.event_kinds import (
    KIND_ICONS,
    PROGRAM_KINDS,
    EventKind,
    event_kind,
    is_program,
)
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.infrastructure.terricon.normalization import (
    normalize_http_url,
    registration_link,
)
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.callbacks import (
    CatalogAllCallback,
    CatalogCategoryCallback,
    CatalogFormatAllCallback,
    CatalogFormatCallback,
    CatalogLanguageAllCallback,
    CatalogLanguageCallback,
    CatalogPageCallback,
    CatalogPeriodCallback,
    CatalogResetCallback,
    CatalogSectionCallback,
    ChooseLocaleCallback,
    ContinueOnboardingCallback,
    DeleteProfileCallback,
    EventAction,
    EventCallback,
    FeedbackAction,
    FeedbackActionCallback,
    FeedbackKindCallback,
    NavigationCallback,
    NavigationView,
    NoopCallback,
    ProgramKindAllCallback,
    ProgramKindCallback,
    SettingLocaleCallback,
    SettingToggleCallback,
    SubscriptionAllCallback,
    SubscriptionCategoryCallback,
    SubscriptionProgramsCallback,
    ToggleSetting,
)
from terricon_events_bot.telegram.rendering import Screen

OFFICIAL_SITE_URL = "https://terricon.kz/"
_BUTTON_TEXT_LIMIT = 60
_TITLE_LIMIT = 180
_DESCRIPTION_CHUNK_LIMIT = 2800
_SCREEN_TEXT_LIMIT = 4096
_MENU_ROWS = (
    (
        ("menu.events", NavigationView.EVENTS, "🗓"),
        ("menu.programs", NavigationView.PROGRAMS, "🚀"),
    ),
    (
        ("menu.subscriptions", NavigationView.SUBSCRIPTIONS, "🔔"),
        ("menu.settings", NavigationView.SETTINGS, "⚙️"),
    ),
    (
        ("menu.feedback", NavigationView.FEEDBACK, "💬"),
        ("menu.about", NavigationView.ABOUT, "ℹ️"),  # noqa: RUF001
    ),
)
_SECTION_ICONS = {CatalogSection.UPCOMING: "🗓", CatalogSection.ARCHIVE: "📦"}
_PROGRAM_KIND_KEYS = {
    EventKind.ACCELERATOR: "programs.accelerators",
    EventKind.INCUBATOR: "programs.incubators",
    EventKind.COURSE: "programs.courses",
}
_FEEDBACK_KIND_ICONS = {
    FeedbackKind.ERROR: "🐞",
    FeedbackKind.IDEA: "💡",
    FeedbackKind.OTHER: "✉️",
}


class TelegramViews:
    def __init__(
        self,
        localizations: LocalizationCatalog,
        assets: AssetResolver,
        *,
        timezone: ZoneInfo,
    ) -> None:
        self._localizations = localizations
        self._assets = assets
        self._timezone = timezone

    def onboarding_language(self) -> Screen:
        keyboard = self._keyboard(
            (
                (
                    self._callback_button("Русский", ChooseLocaleCallback(locale=Locale.RU)),
                    self._callback_button("Қазақша", ChooseLocaleCallback(locale=Locale.KZ)),
                ),
            )
        )
        # The interface language is unknown yet, so the prompt is bilingual.
        prompt = "\n".join(
            self._escape(self._text(locale, "onboarding.choose_language"))
            for locale in (Locale.RU, Locale.KZ)
        )
        return Screen(text=prompt, keyboard=keyboard)

    def onboarding_intro(self, locale: Locale) -> Screen:
        return Screen(
            text=self._escape(self._text(locale, "onboarding.intro")),
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            self._text(locale, "common.continue"),
                            ContinueOnboardingCallback(),
                        ),
                    ),
                )
            ),
        )

    def main_menu(self, user: User) -> Screen:
        locale = user.locale
        rows = tuple(
            tuple(
                self._callback_button(
                    f"{icon} {self._text(locale, key)}", NavigationCallback(view=view)
                )
                for key, view, icon in row
            )
            for row in _MENU_ROWS
        )
        return Screen(
            text=(
                f"<b>{self._escape(self._text(locale, 'menu.title'))}</b>\n"
                f"{self._escape(self._text(locale, 'menu.subtitle'))}"
            ),
            keyboard=self._keyboard(rows),
            image=self._assets.main(locale) if user.menu_images_enabled else None,
        )

    def categories(self, user: User, state: CatalogState) -> Screen:
        locale = user.locale
        current = state.filters.category
        rows: list[tuple[InlineKeyboardButton, ...]] = [
            (
                self._callback_button(
                    self._selected(self._text(locale, "events.all_categories"), current is None),
                    CatalogAllCallback(),
                ),
            )
        ]
        rows.extend(
            (
                self._callback_button(
                    self._selected(
                        self._text(locale, f"categories.{category.value}"),
                        category is current,
                    ),
                    CatalogCategoryCallback(category=category),
                ),
            )
            for category in CategorySlug
        )
        rows.append(self._list_back_row(locale, state.page))
        return Screen(
            text=(
                f"<b>{self._escape(self._text(locale, 'events.category'))}</b>\n"
                f"{self._escape(self._text(locale, 'events.category_hint'))}"
            ),
            keyboard=self._keyboard(rows),
        )

    def catalog_list(self, user: User, state: CatalogState, page: CatalogPage) -> Screen:
        if state.filters.scope is CatalogScope.PROGRAMS:
            return self._programs_list(user, state, page)
        locale = user.locale
        filters = state.filters
        lines = [
            f"<b>{self._escape(self._section_title(locale, filters.section))}</b> · "
            f"{self._escape(self._category_name(locale, filters.category))}",
            f"🔎 {self._escape(self._filter_summary(locale, filters))}",
        ]
        if page.stale:
            lines.extend(("", f"⚠️ {self._escape(self._text(locale, 'common.stale_data'))}"))
        if not page.items:
            lines.extend(("", self._escape(self._text(locale, "events.empty"))))
        else:
            lines.extend(self._list_items(locale, page))

        rows = self._list_rows(page)
        if not page.items and self._filters_narrowed(filters):
            rows.append(
                (
                    self._callback_button(
                        f"♻️ {self._text(locale, 'events.reset_filters')}",
                        CatalogResetCallback(view=NavigationView.EVENTS),
                    ),
                )
            )
        other_section = (
            CatalogSection.ARCHIVE
            if filters.section is CatalogSection.UPCOMING
            else CatalogSection.UPCOMING
        )
        rows.extend(
            (
                (
                    self._callback_button(
                        f"🗂 {self._text(locale, 'events.category')}",
                        NavigationCallback(view=NavigationView.CATEGORIES),
                    ),
                    self._callback_button(
                        f"🔎 {self._text(locale, 'filters.title')}",
                        NavigationCallback(view=NavigationView.FILTERS),
                    ),
                ),
                (
                    self._callback_button(
                        self._section_title(locale, other_section),
                        CatalogSectionCallback(section=other_section),
                    ),
                    self._menu_button(locale),
                ),
            )
        )
        image: Path | None = None
        if user.menu_images_enabled and filters.category is not None:
            image = self._assets.category(locale, filters.category)
        return Screen(text="\n".join(lines), keyboard=self._keyboard(rows), image=image)

    def _programs_list(self, user: User, state: CatalogState, page: CatalogPage) -> Screen:
        locale = user.locale
        filters = state.filters
        kind_name = (
            self._text(locale, _PROGRAM_KIND_KEYS[filters.program_kind])
            if filters.program_kind is not None
            else self._text(locale, "programs.all_kinds")
        )
        lines = [
            f"<b>🚀 {self._escape(self._text(locale, 'programs.title'))}</b> · "
            f"{self._escape(self._section_title(locale, filters.section))} · "
            f"{self._escape(kind_name)}",
            self._escape(self._text(locale, "programs.hint")),
        ]
        if page.stale:
            lines.extend(("", f"⚠️ {self._escape(self._text(locale, 'common.stale_data'))}"))
        if not page.items:
            lines.extend(("", self._escape(self._text(locale, "programs.empty"))))
        else:
            lines.extend(self._list_items(locale, page))
        rows = self._list_rows(page)
        kinds = [
            self._callback_button(
                self._selected(
                    self._text(locale, "programs.all_kinds"), filters.program_kind is None
                ),
                ProgramKindAllCallback(),
            ),
            *(
                self._callback_button(
                    self._selected(
                        f"{KIND_ICONS[kind]} {self._text(locale, _PROGRAM_KIND_KEYS[kind])}",
                        filters.program_kind is kind,
                    ),
                    ProgramKindCallback(kind=kind),
                )
                for kind in PROGRAM_KINDS
            ),
        ]
        rows.extend((tuple(kinds[:2]), tuple(kinds[2:])))
        other_section = (
            CatalogSection.ARCHIVE
            if filters.section is CatalogSection.UPCOMING
            else CatalogSection.UPCOMING
        )
        rows.append(
            (
                self._callback_button(
                    self._section_title(locale, other_section),
                    CatalogSectionCallback(section=other_section),
                ),
                self._menu_button(locale),
            )
        )
        return Screen(text="\n".join(lines), keyboard=self._keyboard(rows))

    def _list_items(self, locale: Locale, page: CatalogPage) -> list[str]:
        items: list[str] = []
        for index, event in enumerate(page.items, start=1):
            kind = event_kind(event.event_type)
            badge = (
                f"{KIND_ICONS[kind]} {self._escape(self._text(locale, f'kinds.{kind.value}'))} · "
                if kind is not None
                else ""
            )
            items.append(
                f"\n<b>{index}.</b> {self._escape(self._short(event.title, _TITLE_LIMIT))}\n"
                f"{badge}📅 {self._escape(self._format_date(event.starts_at))} · "
                f"{self._escape(self._format_name(locale, event.event_format))}"
            )
        return items

    def _list_rows(self, page: CatalogPage) -> list[tuple[InlineKeyboardButton, ...]]:
        rows: list[tuple[InlineKeyboardButton, ...]] = [
            (
                self._callback_button(
                    f"{index}. {self._short(event.title, _BUTTON_TEXT_LIMIT)}",
                    EventCallback(
                        event_id=event.id,
                        action=EventAction.CARD,
                        list_page=page.page,
                    ),
                ),
            )
            for index, event in enumerate(page.items, start=1)
        ]
        pagination = self._pagination_row(
            page.page,
            page.pages,
            lambda number: CatalogPageCallback(page=number),
        )
        if pagination:
            rows.append(pagination)
        return rows

    def filters(self, user: User, state: CatalogState) -> Screen:
        locale = user.locale
        filters = state.filters
        allowed_periods = (
            (CatalogPeriod.TODAY, CatalogPeriod.DAYS_7, CatalogPeriod.DAYS_30, CatalogPeriod.ALL)
            if filters.section is CatalogSection.UPCOMING
            else (CatalogPeriod.DAYS_30, CatalogPeriod.MONTHS_3, CatalogPeriod.ALL)
        )
        rows: list[tuple[InlineKeyboardButton, ...]] = [
            tuple(
                self._callback_button(
                    self._selected(
                        self._period_name(locale, filters.section, period),
                        period is filters.period,
                    ),
                    CatalogPeriodCallback(period=period),
                )
                for period in allowed_periods
            ),
            (
                self._callback_button(
                    self._selected(
                        self._text(locale, "filters.format_all"),
                        filters.event_format is None,
                    ),
                    CatalogFormatAllCallback(),
                ),
            ),
            tuple(
                self._callback_button(
                    self._selected(
                        self._format_name(locale, event_format),
                        filters.event_format is event_format,
                    ),
                    CatalogFormatCallback(event_format=event_format),
                )
                for event_format in (EventFormat.ONLINE, EventFormat.OFFLINE, EventFormat.HYBRID)
            ),
            (
                self._callback_button(
                    self._selected(
                        self._text(locale, "filters.language_all"),
                        filters.language is None,
                    ),
                    CatalogLanguageAllCallback(),
                ),
            ),
            tuple(
                self._callback_button(
                    self._selected(
                        self._text(locale, f"languages.{language.value}"),
                        filters.language is language,
                    ),
                    CatalogLanguageCallback(language=language),
                )
                for language in EventLanguage
            ),
        ]
        if self._filters_narrowed(filters):
            rows.append(
                (
                    self._callback_button(
                        f"♻️ {self._text(locale, 'events.reset_filters')}",
                        CatalogResetCallback(view=NavigationView.FILTERS),
                    ),
                )
            )
        rows.append(
            (
                self._callback_button(
                    f"✅ {self._text(locale, 'filters.show')}",
                    CatalogPageCallback(page=state.page),
                ),
            )
        )
        return Screen(
            text=(
                f"<b>{self._escape(self._text(locale, 'filters.title'))}</b> · "
                f"{self._escape(self._section_title(locale, filters.section))}\n"
                f"{self._escape(self._text(locale, 'filters.hint'))}"
            ),
            keyboard=self._keyboard(rows),
        )

    def event_card(
        self,
        user: User,
        state: CatalogState,
        event: EventDetails,
        *,
        page: int,
        previous: tuple[int, int] | None,
        following: tuple[int, int] | None,
    ) -> Screen:
        locale = user.locale
        kind = event_kind(event.event_type)
        program = is_program(event.event_type)
        categories = ", ".join(
            self._text(locale, f"categories.{category.value}") for category in event.categories
        )
        language_names = ", ".join(
            self._language_name(locale, language) for language in event.event_languages
        )
        lines = [f"<b>{self._escape(self._short(event.title, _TITLE_LIMIT))}</b>", ""]
        if kind is not None:
            lines.append(
                self._field(
                    locale,
                    "events.kind",
                    self._text(locale, f"kinds.{kind.value}"),
                    KIND_ICONS[kind],
                )
            )
        lines.extend(
            (
                self._field(
                    locale,
                    "events.start" if program else "events.date",
                    self._format_date(event.starts_at),
                    "📅",
                ),
                self._field(
                    locale, "events.format", self._format_name(locale, event.event_format), "💻"
                ),
            )
        )
        # Programs and neconf/hackathon records leave these empty at the source.
        optional = (
            ("events.address", event.address, "📍"),
            ("events.language", language_names, "🗣"),
            ("events.categories", None if program else categories, "🏷"),
        )
        for key, value, icon in optional:
            if value or kind is None:
                lines.append(self._field(locale, key, value, icon))
        if event.translation_source is TranslationSource.MACHINE:
            lines.extend(
                ("", f"🤖 {self._escape(self._text(locale, 'events.machine_translation'))}")
            )
        rows: list[tuple[InlineKeyboardButton, ...]] = [
            (
                self._callback_button(
                    f"📄 {self._text(locale, 'events.description')}",
                    EventCallback(
                        event_id=event.id,
                        action=EventAction.DESCRIPTION,
                        list_page=page,
                    ),
                ),
            )
        ]
        external = self._external_button(locale, state.filters.section, event, program=program)
        if external is not None:
            rows.append((external,))
        navigation: list[InlineKeyboardButton] = []
        if previous is not None:
            navigation.append(
                self._callback_button(
                    "◀️",
                    EventCallback(
                        event_id=previous[0],
                        action=EventAction.CARD,
                        list_page=previous[1],
                    ),
                )
            )
        navigation.append(
            self._callback_button(
                f"📋 {self._text(locale, 'events.list_back')}",
                CatalogPageCallback(page=page),
            )
        )
        if following is not None:
            navigation.append(
                self._callback_button(
                    "▶️",
                    EventCallback(
                        event_id=following[0],
                        action=EventAction.CARD,
                        list_page=following[1],
                    ),
                )
            )
        rows.append(tuple(navigation))
        poster = safe_http_url(event.poster_url) if user.event_posters_enabled else None
        return Screen(text="\n".join(lines), keyboard=self._keyboard(rows), image=poster)

    def description(
        self, user: User, event: EventDetails, *, list_page: int, content_page: int
    ) -> Screen:
        locale = user.locale
        chunks = split_html_chunks(event.description or self._text(locale, "common.unavailable"))
        selected_page = min(max(content_page, 1), len(chunks))
        lines = [
            f"<b>{self._escape(self._short(event.title, _TITLE_LIMIT))}</b>",
            "",
            chunks[selected_page - 1],
        ]
        if event.audience:
            lines.extend(("", self._field(locale, "events.audience", event.audience)))
        if event.speaker:
            lines.append(self._field(locale, "events.speaker", event.speaker))
        if event.translation_source is TranslationSource.MACHINE:
            lines.extend(
                ("", f"🤖 {self._escape(self._text(locale, 'events.machine_translation'))}")
            )

        rows: list[tuple[InlineKeyboardButton, ...]] = []
        pagination = self._pagination_row(
            selected_page,
            len(chunks),
            lambda number: EventCallback(
                event_id=event.id,
                action=EventAction.DESCRIPTION,
                list_page=list_page,
                content_page=number,
            ),
        )
        if pagination:
            rows.append(pagination)
        rows.append(
            (
                self._callback_button(
                    f"⬅️ {self._text(locale, 'events.card_back')}",
                    EventCallback(
                        event_id=event.id,
                        action=EventAction.CARD,
                        list_page=list_page,
                    ),
                ),
            )
        )
        return Screen(text="\n".join(lines), keyboard=self._keyboard(rows))

    def settings(self, user: User) -> Screen:
        locale = user.locale
        enabled = self._text(locale, "settings.enabled")
        disabled = self._text(locale, "settings.disabled")
        return Screen(
            text=(
                f"<b>{self._escape(self._text(locale, 'settings.title'))}</b>\n\n"
                f"{self._escape(self._text(locale, 'settings.language'))}: "
                f"{self._escape(self._text(locale, f'languages.{locale.value}'))}"
            ),
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            self._selected("Русский", locale is Locale.RU),
                            SettingLocaleCallback(locale=Locale.RU),
                        ),
                        self._callback_button(
                            self._selected("Қазақша", locale is Locale.KZ),
                            SettingLocaleCallback(locale=Locale.KZ),
                        ),
                    ),
                    (
                        self._callback_button(
                            f"{self._text(locale, 'settings.menu_images')}: "
                            f"{enabled if user.menu_images_enabled else disabled}",
                            SettingToggleCallback(setting=ToggleSetting.MENU_IMAGES),
                        ),
                    ),
                    (
                        self._callback_button(
                            f"{self._text(locale, 'settings.event_posters')}: "
                            f"{enabled if user.event_posters_enabled else disabled}",
                            SettingToggleCallback(setting=ToggleSetting.EVENT_POSTERS),
                        ),
                    ),
                    (
                        self._callback_button(
                            f"🔒 {self._text(locale, 'settings.privacy')}",
                            NavigationCallback(view=NavigationView.PRIVACY),
                        ),
                    ),
                    (self._menu_button(locale),),
                )
            ),
        )

    def subscriptions(self, user: User, summary: SubscriptionSummary) -> Screen:
        locale = user.locale
        if summary.subscribe_all:
            body = f"• {self._escape(self._text(locale, 'subscriptions.all_events'))}"
        elif summary.categories:
            body = "\n".join(
                f"• {self._escape(self._text(locale, f'categories.{category.value}'))}"
                for category in summary.categories
            )
        else:
            body = ""
        if summary.programs:
            programs_line = f"• 🚀 {self._escape(self._text(locale, 'subscriptions.programs'))}"
            body = f"{body}\n{programs_line}" if body else programs_line
        if not body:
            body = self._escape(self._text(locale, "subscriptions.none"))
        selected = set(summary.categories)
        rows: list[tuple[InlineKeyboardButton, ...]] = [
            (
                self._callback_button(
                    self._selected(
                        self._text(locale, "subscriptions.all_events"),
                        summary.subscribe_all,
                    ),
                    SubscriptionAllCallback(),
                ),
            )
        ]
        rows.extend(
            (
                self._callback_button(
                    self._selected(
                        self._text(locale, f"categories.{category.value}"),
                        category in selected,
                    ),
                    SubscriptionCategoryCallback(category=category),
                ),
            )
            for category in CategorySlug
        )
        rows.append(
            (
                self._callback_button(
                    self._selected(
                        f"🚀 {self._text(locale, 'subscriptions.programs')}", summary.programs
                    ),
                    SubscriptionProgramsCallback(),
                ),
            )
        )
        rows.append((self._menu_button(locale),))
        return Screen(
            text=(
                f"<b>{self._escape(self._text(locale, 'subscriptions.title'))}</b>\n"
                f"{self._escape(self._text(locale, 'subscriptions.hint'))}\n"
                f"{self._escape(self._text(locale, 'subscriptions.programs_hint'))}\n\n"
                f"<b>{self._escape(self._text(locale, 'subscriptions.current'))}:</b>\n{body}"
            ),
            keyboard=self._keyboard(rows),
        )

    def privacy(self, user: User, *, warning: bool = False) -> Screen:
        locale = user.locale
        rows: tuple[tuple[InlineKeyboardButton, ...], ...]
        if warning:
            text = self._escape(self._text(locale, "privacy.delete_warning"))
            rows = (
                (
                    self._callback_button(
                        f"🗑 {self._text(locale, 'privacy.delete_confirm')}",
                        DeleteProfileCallback(),
                    ),
                ),
                (
                    self._callback_button(
                        self._text(locale, "common.cancel"),
                        NavigationCallback(view=NavigationView.PRIVACY),
                    ),
                ),
            )
        else:
            text = (
                f"<b>{self._escape(self._text(locale, 'privacy.title'))}</b>\n\n"
                f"{self._escape(self._text(locale, 'privacy.details'))}"
            )
            rows = (
                (
                    self._callback_button(
                        f"🗑 {self._text(locale, 'privacy.delete_profile')}",
                        NavigationCallback(view=NavigationView.PRIVACY_WARNING),
                    ),
                ),
                self._back_row(locale, NavigationView.SETTINGS),
            )
        return Screen(text=text, keyboard=self._keyboard(rows))

    def profile_deleted(self, user: User) -> Screen:
        locale = user.locale
        return Screen(
            text=self._escape(self._text(locale, "privacy.deleted")),
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            f"▶️ {self._text(locale, 'privacy.restart')}",
                            NavigationCallback(view=NavigationView.MENU),
                        ),
                    ),
                )
            ),
        )

    def feedback(self, user: User) -> Screen:
        locale = user.locale
        return Screen(
            text=(
                f"<b>{self._escape(self._text(locale, 'feedback.title'))}</b>\n"
                f"{self._escape(self._text(locale, 'feedback.hint'))}"
            ),
            keyboard=self._keyboard(
                (
                    tuple(
                        self._callback_button(
                            f"{icon} {self._text(locale, f'feedback.kind_{kind.value}')}",
                            FeedbackKindCallback(kind=kind),
                        )
                        for kind, icon in _FEEDBACK_KIND_ICONS.items()
                    ),
                    (self._menu_button(locale),),
                )
            ),
        )

    def feedback_text_prompt(self, user: User) -> Screen:
        return self._feedback_step(user, "feedback.enter_text")

    def feedback_text_invalid(self, user: User) -> Screen:
        return self._feedback_step(user, "feedback.text_invalid")

    def feedback_photo_prompt(self, user: User) -> Screen:
        locale = user.locale
        return Screen(
            text=self._escape(self._text(locale, "feedback.attach_photo")),
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            self._text(locale, "feedback.skip_photo"),
                            FeedbackActionCallback(action=FeedbackAction.SKIP_PHOTO),
                        ),
                    ),
                    (
                        self._callback_button(
                            self._text(locale, "common.cancel"),
                            FeedbackActionCallback(action=FeedbackAction.CANCEL),
                        ),
                    ),
                )
            ),
        )

    def feedback_preview(self, user: User, draft: FeedbackDraft) -> Screen:
        locale = user.locale
        kind = self._text(locale, f"feedback.kind_{draft.kind.value}")
        photo = self._text(locale, "common.yes" if draft.telegram_file_id else "common.no")
        missing = self._text(locale, "feedback.not_specified")
        display_name = self._escape(user.display_name or missing)
        username = self._escape(f"@{user.username}" if user.username else missing)
        prefix = (
            f"<b>{self._escape(self._text(locale, 'feedback.preview'))}</b>\n\n"
            f"{self._escape(self._text(locale, 'feedback.preview_name'))}: {display_name}\n"
            f"{self._escape(self._text(locale, 'feedback.preview_username'))}: {username}\n\n"
            f"{self._escape(kind)}\n\n"
        )
        suffix = f"\n\n📷 {self._escape(photo)}"
        body_value = draft.body or ""
        body = self._escape(body_value)
        body_limit = _SCREEN_TEXT_LIMIT - len(prefix) - len(suffix)
        if len(body) > body_limit:
            body = f"{split_html_chunks(body_value, limit=body_limit - 1)[0]}…"
        text = f"{prefix}{body}{suffix}"
        return Screen(
            text=text,
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            f"📨 {self._text(locale, 'feedback.send')}",
                            FeedbackActionCallback(action=FeedbackAction.CONFIRM),
                        ),
                    ),
                    (
                        self._callback_button(
                            self._text(locale, "common.cancel"),
                            FeedbackActionCallback(action=FeedbackAction.CANCEL),
                        ),
                    ),
                )
            ),
        )

    def feedback_notice(self, user: User, key: str) -> Screen:
        return Screen(
            text=self._escape(self._text(user.locale, key)),
            keyboard=self._keyboard(((self._menu_button(user.locale),),)),
        )

    def _feedback_step(self, user: User, key: str) -> Screen:
        locale = user.locale
        return Screen(
            text=self._escape(self._text(locale, key)),
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            self._text(locale, "common.cancel"),
                            FeedbackActionCallback(action=FeedbackAction.CANCEL),
                        ),
                    ),
                )
            ),
        )

    def about(self, user: User) -> Screen:
        locale = user.locale
        return Screen(
            text=self._escape(self._text(locale, "about.text")),
            keyboard=self._keyboard(
                (
                    (
                        InlineKeyboardButton(
                            text=f"🌐 {self._text(locale, 'about.official_site')}",
                            url=OFFICIAL_SITE_URL,
                        ),
                    ),
                    (self._menu_button(locale),),
                )
            ),
        )

    def invalid_action(self, user: User) -> Screen:
        return Screen(
            text=self._escape(self._text(user.locale, "errors.invalid_action")),
            keyboard=self._keyboard(((self._menu_button(user.locale),),)),
        )

    @staticmethod
    def _keyboard(
        rows: Iterable[Sequence[InlineKeyboardButton]],
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])

    @staticmethod
    def _callback_button(text: str, callback: CallbackData) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=callback.pack())

    def _back_row(self, locale: Locale, view: NavigationView) -> tuple[InlineKeyboardButton, ...]:
        return (
            self._callback_button(
                f"⬅️ {self._text(locale, 'common.back')}", NavigationCallback(view=view)
            ),
        )

    def _list_back_row(self, locale: Locale, page: int) -> tuple[InlineKeyboardButton, ...]:
        return (
            self._callback_button(
                f"⬅️ {self._text(locale, 'events.list_back')}", CatalogPageCallback(page=page)
            ),
        )

    def _menu_button(self, locale: Locale) -> InlineKeyboardButton:
        return self._callback_button(
            f"🏠 {self._text(locale, 'common.menu')}", NavigationCallback(view=NavigationView.MENU)
        )

    def _pagination_row(
        self, current: int, total: int, target: Callable[[int], CallbackData]
    ) -> tuple[InlineKeyboardButton, ...]:
        if total <= 1:
            return ()
        row: list[InlineKeyboardButton] = []
        if current > 1:
            row.append(self._callback_button("◀️", target(current - 1)))
        row.append(self._callback_button(f"{current}/{total}", NoopCallback()))
        if current < total:
            row.append(self._callback_button("▶️", target(current + 1)))
        return tuple(row)

    def _section_title(self, locale: Locale, section: CatalogSection) -> str:
        key = "events.upcoming" if section is CatalogSection.UPCOMING else "events.archive"
        return f"{_SECTION_ICONS[section]} {self._text(locale, key)}"

    def _category_name(self, locale: Locale, category: CategorySlug | None) -> str:
        if category is None:
            return self._text(locale, "events.all_categories")
        return self._text(locale, f"categories.{category.value}")

    def _filter_summary(self, locale: Locale, filters: CatalogFilter) -> str:
        event_format = (
            self._format_name(locale, filters.event_format)
            if filters.event_format is not None
            else self._text(locale, "filters.format_all")
        )
        language = (
            self._text(locale, f"languages.{filters.language.value}")
            if filters.language is not None
            else self._text(locale, "filters.language_all")
        )
        return " · ".join(
            (self._period_name(locale, filters.section, filters.period), event_format, language)
        )

    @staticmethod
    def _filters_narrowed(filters: CatalogFilter) -> bool:
        return (
            filters.period is not CatalogPeriod.ALL
            or filters.event_format is not None
            or filters.language is not None
        )

    def _external_button(
        self,
        locale: Locale,
        section: CatalogSection,
        event: EventDetails,
        *,
        program: bool = False,
    ) -> InlineKeyboardButton | None:
        if section is CatalogSection.UPCOMING:
            url = registration_link(event.registration_url, event.details_url)
            key, icon = ("programs.apply" if program else "events.registration"), "📝"
            if url is None:
                # The source closes registration early for streamed events and
                # publishes only the broadcast link.
                url = safe_http_url(event.recording_url)
                key, icon = "events.details", "🔗"
        else:
            recording = safe_http_url(event.recording_url)
            url = recording or safe_http_url(event.details_url, keep_fragment=True)
            key, icon = ("events.recording", "🎬") if recording else ("events.details", "🔗")
        if url is None:
            return None
        return InlineKeyboardButton(text=f"{icon} {self._text(locale, key)}", url=url)

    def _field(self, locale: Locale, key: str, value: str | None, icon: str | None = None) -> str:
        content = value or self._text(locale, "common.unavailable")
        label = self._escape(self._text(locale, key))
        prefix = f"{icon} " if icon else ""
        return f"{prefix}<b>{label}:</b> {self._escape(self._short(content, 300))}"

    def _period_name(self, locale: Locale, section: CatalogSection, period: CatalogPeriod) -> str:
        if period is CatalogPeriod.TODAY:
            return self._text(locale, "filters.today")
        if period is CatalogPeriod.DAYS_7:
            return self._text(locale, "filters.seven_days")
        if period is CatalogPeriod.DAYS_30:
            return self._text(locale, "filters.thirty_days")
        if period is CatalogPeriod.MONTHS_3:
            return self._text(locale, "filters.three_months")
        return (
            self._text(locale, "filters.any_date")
            if section is CatalogSection.UPCOMING
            else self._text(locale, "filters.all_time")
        )

    def _format_name(self, locale: Locale, value: EventFormat) -> str:
        return self._text(locale, f"formats.{value.value}")

    def _language_name(self, locale: Locale, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in {language.value for language in EventLanguage}:
            return self._text(locale, f"languages.{normalized}")
        return self._short(value, 32)

    def _format_date(self, value: datetime) -> str:
        return value.astimezone(self._timezone).strftime("%d.%m.%Y %H:%M")

    def _text(self, locale: Locale, key: str) -> str:
        return self._localizations.get(locale, key)

    @staticmethod
    def _selected(text: str, selected: bool) -> str:
        return f"✓ {text}" if selected else text

    @staticmethod
    def _short(value: str, limit: int) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 1].rstrip()}…"

    @staticmethod
    def _escape(value: str) -> str:
        return html.escape(value, quote=False)


def safe_http_url(value: str | None, *, keep_fragment: bool = False) -> str | None:
    return normalize_http_url(value, keep_fragment=keep_fragment) if value is not None else None


def split_html_chunks(value: str, limit: int = _DESCRIPTION_CHUNK_LIMIT) -> tuple[str, ...]:
    if limit <= 0:
        raise ValueError("Description chunk limit must be positive")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ("",)
    chunks: list[str] = []
    remaining = normalized
    while remaining:
        low = 1
        high = min(len(remaining), limit)
        best = 0
        while low <= high:
            middle = (low + high) // 2
            if len(html.escape(remaining[:middle], quote=False)) <= limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == 0:
            raise ValueError("Description chunk limit is too small for escaped text")
        split_at = best
        if split_at < len(remaining):
            natural = max(remaining.rfind("\n", 0, split_at), remaining.rfind(" ", 0, split_at))
            if natural >= split_at // 2:
                split_at = natural
        raw_chunk = remaining[:split_at].strip()
        if not raw_chunk:
            raw_chunk = remaining[:best]
            split_at = best
        chunks.append(html.escape(raw_chunk, quote=False))
        remaining = remaining[split_at:].lstrip()
    return tuple(chunks)
