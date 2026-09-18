from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from terricon_events_bot.application.subscriptions import SubscriptionSummary
from terricon_events_bot.domain.catalog import (
    CatalogPage,
    CatalogPeriod,
    CatalogSection,
    CatalogState,
    EventDetails,
    EventLanguage,
)
from terricon_events_bot.domain.enums import CategorySlug, EventFormat, Locale, TranslationSource
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.infrastructure.terricon.normalization import normalize_http_url
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
    CatalogSectionCallback,
    ChooseLocaleCallback,
    ContinueOnboardingCallback,
    EventAction,
    EventCallback,
    NavigationCallback,
    NavigationView,
    SettingLocaleCallback,
    SettingToggleCallback,
    SubscriptionAllCallback,
    SubscriptionCategoryCallback,
    ToggleSetting,
)
from terricon_events_bot.telegram.rendering import Screen

OFFICIAL_SITE_URL = "https://terricon.kz/"
_BUTTON_TEXT_LIMIT = 60
_TITLE_LIMIT = 180
_DESCRIPTION_CHUNK_LIMIT = 2800


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
        return Screen(
            text=self._escape(self._text(Locale.RU, "onboarding.choose_language")),
            keyboard=keyboard,
        )

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
            (self._callback_button(self._text(locale, key), NavigationCallback(view=view)),)
            for key, view in (
                ("menu.events", NavigationView.EVENTS),
                ("menu.subscriptions", NavigationView.SUBSCRIPTIONS),
                ("menu.settings", NavigationView.SETTINGS),
                ("menu.feedback", NavigationView.FEEDBACK),
                ("menu.about", NavigationView.ABOUT),
            )
        )
        return Screen(
            text=f"<b>{self._escape(self._text(locale, 'menu.title'))}</b>",
            keyboard=self._keyboard(rows),
            image=self._assets.main(locale) if user.menu_images_enabled else None,
        )

    def event_sections(self, user: User) -> Screen:
        locale = user.locale
        return Screen(
            text=f"<b>{self._escape(self._text(locale, 'events.title'))}</b>",
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            self._text(locale, "events.upcoming"),
                            CatalogSectionCallback(section=CatalogSection.UPCOMING),
                        ),
                        self._callback_button(
                            self._text(locale, "events.archive"),
                            CatalogSectionCallback(section=CatalogSection.ARCHIVE),
                        ),
                    ),
                    self._back_row(locale, NavigationView.MENU),
                )
            ),
        )

    def categories(self, user: User, section: CatalogSection) -> Screen:
        locale = user.locale
        rows: list[tuple[InlineKeyboardButton, ...]] = [
            (
                self._callback_button(
                    self._text(locale, "events.all"),
                    CatalogAllCallback(section=section),
                ),
            )
        ]
        rows.extend(
            (
                self._callback_button(
                    self._text(locale, f"categories.{category.value}"),
                    CatalogCategoryCallback(section=section, category=category),
                ),
            )
            for category in CategorySlug
        )
        rows.append(self._back_row(locale, NavigationView.EVENTS))
        title_key = "events.upcoming" if section is CatalogSection.UPCOMING else "events.archive"
        return Screen(
            text=(
                f"<b>{self._escape(self._text(locale, title_key))}</b>\n"
                f"{self._escape(self._text(locale, 'events.all'))}"
            ),
            keyboard=self._keyboard(rows),
        )

    def catalog_list(self, user: User, state: CatalogState, page: CatalogPage) -> Screen:
        locale = user.locale
        section_key = (
            "events.upcoming"
            if state.filters.section is CatalogSection.UPCOMING
            else "events.archive"
        )
        lines = [f"<b>{self._escape(self._text(locale, section_key))}</b>"]
        if state.filters.category is not None:
            lines.append(
                self._escape(self._text(locale, f"categories.{state.filters.category.value}"))
            )
        if page.stale:
            lines.extend(("", f"⚠️ {self._escape(self._text(locale, 'common.stale_data'))}"))
        if not page.items:
            lines.extend(("", self._escape(self._text(locale, "events.empty"))))
        else:
            for index, event in enumerate(page.items, start=1):
                lines.append(
                    f"\n{index}. {self._escape(self._short(event.title, _TITLE_LIMIT))}\n"
                    f"   {self._escape(self._format_date(event.starts_at))} · "
                    f"{self._escape(self._format_name(locale, event.event_format))}"
                )
        lines.append(f"\n{page.page}/{page.pages}")

        rows: list[tuple[InlineKeyboardButton, ...]] = [
            (
                self._callback_button(
                    self._short(event.title, _BUTTON_TEXT_LIMIT),
                    EventCallback(
                        event_id=event.id,
                        action=EventAction.CARD,
                        list_page=page.page,
                    ),
                ),
            )
            for event in page.items
        ]
        pagination: list[InlineKeyboardButton] = []
        if page.page > 1:
            pagination.append(
                self._callback_button(
                    self._text(locale, "common.back"),
                    CatalogPageCallback(page=page.page - 1),
                )
            )
        if page.page < page.pages:
            pagination.append(
                self._callback_button(
                    self._text(locale, "common.next"),
                    CatalogPageCallback(page=page.page + 1),
                )
            )
        if pagination:
            rows.append(tuple(pagination))
        rows.extend(
            (
                (
                    self._callback_button(
                        self._text(locale, "filters.title"),
                        NavigationCallback(view=NavigationView.FILTERS),
                    ),
                ),
                self._back_row(locale, NavigationView.EVENTS),
            )
        )
        image: Path | None = None
        if user.menu_images_enabled and state.filters.category is not None:
            image = self._assets.category(locale, state.filters.category)
        return Screen(text="\n".join(lines), keyboard=self._keyboard(rows), image=image)

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
            (
                self._callback_button(
                    self._text(locale, "events.list_back"),
                    CatalogPageCallback(page=state.page),
                ),
            ),
        ]
        return Screen(
            text=f"<b>{self._escape(self._text(locale, 'filters.title'))}</b>",
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
        categories = ", ".join(
            self._text(locale, f"categories.{category.value}") for category in event.categories
        ) or self._text(locale, "common.unavailable")
        language_names = ", ".join(
            self._language_name(locale, language) for language in event.event_languages
        ) or self._text(locale, "common.unavailable")
        lines = [
            f"<b>{self._escape(self._short(event.title, _TITLE_LIMIT))}</b>",
            "",
            self._field(locale, "events.date", self._format_date(event.starts_at)),
            self._field(locale, "events.format", self._format_name(locale, event.event_format)),
            self._field(locale, "events.address", event.address),
            self._field(locale, "events.language", language_names),
            self._field(locale, "events.categories", categories),
        ]
        if event.translation_source is TranslationSource.MACHINE:
            lines.extend(
                ("", f"🤖 {self._escape(self._text(locale, 'events.machine_translation'))}")
            )
        rows: list[tuple[InlineKeyboardButton, ...]] = [
            (
                self._callback_button(
                    self._text(locale, "events.description"),
                    EventCallback(
                        event_id=event.id,
                        action=EventAction.DESCRIPTION,
                        list_page=page,
                    ),
                ),
            )
        ]
        external = self._external_button(locale, state.filters.section, event)
        if external is not None:
            rows.append((external,))
        adjacent: list[InlineKeyboardButton] = []
        if previous is not None:
            adjacent.append(
                self._callback_button(
                    self._text(locale, "common.back"),
                    EventCallback(
                        event_id=previous[0],
                        action=EventAction.CARD,
                        list_page=previous[1],
                    ),
                )
            )
        if following is not None:
            adjacent.append(
                self._callback_button(
                    self._text(locale, "common.next"),
                    EventCallback(
                        event_id=following[0],
                        action=EventAction.CARD,
                        list_page=following[1],
                    ),
                )
            )
        if adjacent:
            rows.append(tuple(adjacent))
        rows.append(
            (
                self._callback_button(
                    self._text(locale, "events.list_back"),
                    CatalogPageCallback(page=page),
                ),
            )
        )
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
        if len(chunks) > 1:
            lines.append(f"\n{selected_page}/{len(chunks)}")

        pagination: list[InlineKeyboardButton] = []
        if selected_page > 1:
            pagination.append(
                self._callback_button(
                    self._text(locale, "common.back"),
                    EventCallback(
                        event_id=event.id,
                        action=EventAction.DESCRIPTION,
                        list_page=list_page,
                        content_page=selected_page - 1,
                    ),
                )
            )
        if selected_page < len(chunks):
            pagination.append(
                self._callback_button(
                    self._text(locale, "common.next"),
                    EventCallback(
                        event_id=event.id,
                        action=EventAction.DESCRIPTION,
                        list_page=list_page,
                        content_page=selected_page + 1,
                    ),
                )
            )
        rows: list[tuple[InlineKeyboardButton, ...]] = []
        if pagination:
            rows.append(tuple(pagination))
        rows.append(
            (
                self._callback_button(
                    self._text(locale, "common.back"),
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
            text=f"<b>{self._escape(self._text(locale, 'settings.title'))}</b>",
            keyboard=self._keyboard(
                (
                    (
                        self._callback_button(
                            self._selected("RU", locale is Locale.RU),
                            SettingLocaleCallback(locale=Locale.RU),
                        ),
                        self._callback_button(
                            self._selected("KZ", locale is Locale.KZ),
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
                            self._text(locale, "settings.privacy"),
                            NavigationCallback(view=NavigationView.PRIVACY),
                        ),
                    ),
                    self._back_row(locale, NavigationView.MENU),
                )
            ),
        )

    def subscriptions(self, user: User, summary: SubscriptionSummary) -> Screen:
        locale = user.locale
        if summary.subscribe_all:
            body = self._text(locale, "subscriptions.all_events")
        elif summary.categories:
            body = "\n".join(
                f"• {self._escape(self._text(locale, f'categories.{category.value}'))}"
                for category in summary.categories
            )
        else:
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
        rows.append(self._back_row(locale, NavigationView.MENU))
        return Screen(
            text=f"<b>{self._escape(self._text(locale, 'subscriptions.title'))}</b>\n\n{body}",
            keyboard=self._keyboard(rows),
        )

    def privacy(self, user: User, *, warning: bool = False) -> Screen:
        locale = user.locale
        text = self._text(locale, "privacy.delete_warning" if warning else "privacy.title")
        rows: list[tuple[InlineKeyboardButton, ...]] = []
        if not warning:
            rows.append(
                (
                    self._callback_button(
                        self._text(locale, "privacy.delete_profile"),
                        NavigationCallback(view=NavigationView.PRIVACY_WARNING),
                    ),
                )
            )
        rows.append(self._back_row(locale, NavigationView.SETTINGS))
        return Screen(text=self._escape(text), keyboard=self._keyboard(rows))

    def feedback(self, user: User) -> Screen:
        locale = user.locale
        return Screen(
            text=f"<b>{self._escape(self._text(locale, 'feedback.title'))}</b>",
            keyboard=self._keyboard((self._back_row(locale, NavigationView.MENU),)),
        )

    def about(self, user: User) -> Screen:
        locale = user.locale
        return Screen(
            text=self._escape(self._text(locale, "about.text")),
            keyboard=self._keyboard(
                (
                    (
                        InlineKeyboardButton(
                            text=self._text(locale, "about.official_site"),
                            url=OFFICIAL_SITE_URL,
                        ),
                    ),
                    self._back_row(locale, NavigationView.MENU),
                )
            ),
        )

    def invalid_action(self, user: User) -> Screen:
        return Screen(
            text=self._escape(self._text(user.locale, "errors.invalid_action")),
            keyboard=self._keyboard((self._back_row(user.locale, NavigationView.MENU),)),
        )

    def with_period(self, state: CatalogState, period: CatalogPeriod) -> CatalogState:
        return state.with_filters(replace(state.filters, period=period))

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
            self._callback_button(self._text(locale, "common.back"), NavigationCallback(view=view)),
        )

    def _external_button(
        self, locale: Locale, section: CatalogSection, event: EventDetails
    ) -> InlineKeyboardButton | None:
        if section is CatalogSection.UPCOMING:
            url = safe_http_url(event.registration_url)
            key = "events.registration"
        else:
            recording = safe_http_url(event.recording_url)
            url = recording or safe_http_url(event.details_url)
            key = "events.recording" if recording else "events.details"
        if url is None:
            return None
        return InlineKeyboardButton(text=self._text(locale, key), url=url)

    def _field(self, locale: Locale, key: str, value: str | None) -> str:
        content = value or self._text(locale, "common.unavailable")
        label = self._escape(self._text(locale, key))
        return f"<b>{label}:</b> {self._escape(self._short(content, 300))}"

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
            self._text(locale, "events.all")
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


def safe_http_url(value: str | None) -> str | None:
    return normalize_http_url(value) if value is not None else None


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
