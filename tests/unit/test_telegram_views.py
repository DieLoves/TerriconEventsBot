from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
    EventSummary,
)
from terricon_events_bot.domain.enums import (
    CategorySlug,
    EventFormat,
    FeedbackKind,
    Locale,
    TranslationSource,
)
from terricon_events_bot.domain.event_kinds import EventKind
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.models import User
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.views import TelegramViews, safe_http_url, split_html_chunks

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def views(assets_root: Path) -> TelegramViews:
    return TelegramViews(
        LocalizationCatalog.load(PROJECT_ROOT / "locales"),
        AssetResolver(assets_root),
        timezone=ZoneInfo("Asia/Almaty"),
    )


def user(*, locale: Locale = Locale.RU, images: bool = True, posters: bool = True) -> User:
    return User(
        id=1,
        telegram_id=100,
        locale=locale,
        menu_images_enabled=images,
        event_posters_enabled=posters,
    )


def event(**overrides: object) -> EventDetails:
    values: dict[str, object] = {
        "id": 1,
        "title": "Safe title",
        "description": "Description",
        "audience": "Audience",
        "speaker": "Speaker",
        "details_url": "https://terricon.kz/event/1",
        "starts_at": datetime(2030, 1, 1, tzinfo=UTC),
        "event_format": EventFormat.OFFLINE,
        "event_languages": ("ru",),
        "address": "Address",
        "registration_url": "https://example.test/register",
        "recording_url": None,
        "poster_url": "https://example.test/poster.jpg",
        "is_available": True,
        "categories": (CategorySlug.DEVELOPMENT_IT,),
        "locale": Locale.RU,
        "translation_source": TranslationSource.OFFICIAL,
    }
    values.update(overrides)
    return EventDetails(**values)  # type: ignore[arg-type]


def test_external_event_text_is_escaped_and_urls_are_restricted(tmp_path: Path) -> None:
    malicious = event(
        title="<script>alert(1)</script>",
        address="<b>fake</b>",
        registration_url="https://user:password@example.test/private",
        poster_url="javascript:alert(1)",
    )
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))

    screen = views(tmp_path).event_card(
        user(), state, malicious, page=1, previous=None, following=None
    )

    assert "<script>" not in screen.text
    assert "&lt;script&gt;" in screen.text
    assert "<b>fake</b>" not in screen.text
    assert screen.image is None
    assert all(button.url is None for row in screen.keyboard.inline_keyboard for button in row)
    assert safe_http_url("https://example.test/a?b=1") == "https://example.test/a?b=1"
    assert safe_http_url("https://example.test/a b") is None
    assert safe_http_url("javascript:alert(1)") is None
    assert safe_http_url("https://user:secret@example.test/") is None
    assert safe_http_url("http://127.0.0.1/private") is None


def test_long_description_is_split_after_html_escaping_and_fits_screen(tmp_path: Path) -> None:
    value = ("<tag>& text " * 1500).strip()
    chunks = split_html_chunks(value)
    telegram_views = views(tmp_path)
    details = event(description=value, audience="A" * 1000, speaker="S" * 1000)

    screens = [
        telegram_views.description(user(), details, list_page=3, content_page=page)
        for page in range(1, len(chunks) + 1)
    ]

    assert len(chunks) > 1
    assert all(len(chunk) <= 2800 for chunk in chunks)
    assert all("<tag>" not in chunk and "&lt;tag&gt;" in chunk for chunk in chunks)
    assert all(len(screen.text) <= 4096 for screen in screens)
    with pytest.raises(ValueError, match="too small"):
        split_html_chunks("<", limit=1)


def test_subscription_view_exposes_all_categories_and_selection(tmp_path: Path) -> None:
    screen = views(tmp_path).subscriptions(
        user(),
        SubscriptionSummary(False, (CategorySlug.AI_DATA, CategorySlug.OTHER)),
    )

    buttons = [button.text for row in screen.keyboard.inline_keyboard for button in row]
    # all-events, categories, programs toggle and the menu button.
    assert len(buttons) == len(CategorySlug) + 3
    assert "✓ AI и Data" in buttons
    assert "✓ Другое" in buttons
    assert "Все новые мероприятия" in buttons  # noqa: RUF001


def test_kz_locale_and_image_preferences_are_applied(tmp_path: Path) -> None:
    main_image = tmp_path / "common" / "main.png"
    category_image = tmp_path / "common" / "categories" / "development_it.jpg"
    main_image.parent.mkdir(parents=True)
    category_image.parent.mkdir(parents=True)
    main_image.write_bytes(b"image")
    category_image.write_bytes(b"image")
    telegram_views = views(tmp_path)
    kz_user = user(locale=Locale.KZ)
    no_images = user(locale=Locale.KZ, images=False, posters=False)
    state = CatalogState(
        CatalogFilter(
            CatalogSection.UPCOMING,
            CatalogPeriod.ALL,
            category=CategorySlug.DEVELOPMENT_IT,
        )
    )
    page = CatalogPage(
        items=(
            EventSummary(
                id=1,
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

    assert telegram_views.main_menu(kz_user).text.startswith("<b>Басты мәзір</b>\n")
    assert telegram_views.main_menu(kz_user).image == main_image
    assert telegram_views.main_menu(no_images).image is None
    assert telegram_views.catalog_list(kz_user, state, page).image == category_image
    assert (
        telegram_views.event_card(
            no_images, state, event(), page=1, previous=None, following=None
        ).image
        is None
    )


def test_feedback_wizard_and_delete_warning_require_confirmation(tmp_path: Path) -> None:
    telegram_views = views(tmp_path)
    current_user = user()
    current_user.display_name = "Preview <Name>"
    current_user.username = "preview_user"

    feedback = telegram_views.feedback(current_user)
    preview = telegram_views.feedback_preview(
        current_user,
        FeedbackDraft(FeedbackKind.ERROR, "Unsafe <text>", "photo-id", "preview"),
    )
    privacy = telegram_views.privacy(current_user, warning=True)

    assert {button.text for row in feedback.keyboard.inline_keyboard for button in row} >= {
        "🐞 Ошибка",
        "💡 Идея",
        "✉️ Другое",
    }
    assert "Unsafe &lt;text&gt;" in preview.text
    assert "Preview &lt;Name&gt;" in preview.text
    assert "@preview_user" in preview.text
    assert "🗑 Да, удалить" in {
        button.text for row in privacy.keyboard.inline_keyboard for button in row
    }


def test_feedback_preview_fits_telegram_after_html_escaping(tmp_path: Path) -> None:
    telegram_views = views(tmp_path)
    current_user = user()
    current_user.display_name = "&" * 255
    current_user.username = "u" * 64

    preview = telegram_views.feedback_preview(
        current_user,
        FeedbackDraft(FeedbackKind.OTHER, "<&" * 1333, None, "preview"),
    )

    assert len(preview.text) <= 4096
    assert "…" in preview.text
    assert "<&" not in preview.text
    assert "📨 Отправить" in {
        button.text for row in preview.keyboard.inline_keyboard for button in row
    }


def test_upcoming_card_without_registration_links_to_announced_stream(tmp_path: Path) -> None:
    stream = "https://www.youtube.com/@TerriconValley/streams"
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))

    screen = views(tmp_path).event_card(
        user(),
        state,
        event(registration_url=None, recording_url=stream),
        page=1,
        previous=None,
        following=None,
    )

    links = [
        (button.text, button.url)
        for row in screen.keyboard.inline_keyboard
        for button in row
        if button.url is not None
    ]
    assert links == [("🔗 Подробнее", stream)]


def summary(event_id: int) -> EventSummary:
    return EventSummary(
        id=event_id,
        title=f"Event {event_id}",
        starts_at=datetime(2030, 1, 1, tzinfo=UTC),
        event_format=EventFormat.OFFLINE,
        categories=(),
        translation_source=TranslationSource.OFFICIAL,
    )


def button_rows(screen: object) -> list[list[str]]:
    keyboard = screen.keyboard  # type: ignore[attr-defined]
    return [[button.text for button in row] for row in keyboard.inline_keyboard]


def test_catalog_list_shows_active_filters_and_unambiguous_navigation(tmp_path: Path) -> None:
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING), page=2)
    listing = CatalogPage(items=(summary(1), summary(2)), page=2, pages=3, total=12, stale=False)

    screen = views(tmp_path).catalog_list(user(), state, listing)

    assert screen.text.startswith("<b>🗓 Актуальные</b> · Все категории\n")  # noqa: RUF001
    assert "Любые даты · Любой формат · Любой язык" in screen.text
    assert button_rows(screen) == [
        ["1. Event 1"],
        ["2. Event 2"],
        ["◀️", "2/3", "▶️"],
        ["🗂 Категория", "🔎 Фильтры"],
        ["📦 Архив", "🏠 В меню"],  # noqa: RUF001
    ]


def test_empty_list_offers_reset_only_when_filters_narrow_it(tmp_path: Path) -> None:
    telegram_views = views(tmp_path)
    empty = CatalogPage(items=(), page=1, pages=1, total=0, stale=False)
    narrowed = CatalogState(
        CatalogFilter(
            CatalogSection.ARCHIVE, CatalogPeriod.DAYS_30, event_format=EventFormat.ONLINE
        )
    )
    default = CatalogState(CatalogFilter.default(CatalogSection.ARCHIVE))

    narrowed_rows = button_rows(telegram_views.catalog_list(user(), narrowed, empty))
    default_rows = button_rows(telegram_views.catalog_list(user(), default, empty))

    assert ["♻️ Сбросить фильтры"] in narrowed_rows
    assert ["♻️ Сбросить фильтры"] not in default_rows
    assert ["🗓 Актуальные", "🏠 В меню"] in default_rows  # noqa: RUF001


def test_card_and_description_navigation_never_reuse_one_label(tmp_path: Path) -> None:
    telegram_views = views(tmp_path)
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))

    card = telegram_views.event_card(
        user(), state, event(), page=1, previous=(5, 1), following=(6, 2)
    )
    description = telegram_views.description(
        user(), event(description="word " * 2000), list_page=1, content_page=1
    )

    assert button_rows(card)[-1] == ["◀️", "📋 К списку", "▶️"]  # noqa: RUF001
    assert button_rows(description) == [["1/4", "▶️"], ["⬅️ К мероприятию"]]  # noqa: RUF001
    for screen in (card, description):
        labels = [text for row in button_rows(screen) for text in row]
        assert len(labels) == len(set(labels))


def test_upcoming_registration_opens_the_site_form_for_the_event(tmp_path: Path) -> None:
    site = "https://terricon.kz/ru/it#aleksanova28.10"
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))

    screen = views(tmp_path).event_card(
        user(),
        state,
        event(registration_url="https://share.kz/gBZ5", details_url=site),
        page=1,
        previous=None,
        following=None,
    )

    links = [
        (button.text, button.url)
        for row in screen.keyboard.inline_keyboard
        for button in row
        if button.url is not None
    ]
    assert links == [("📝 Регистрация", site)]


def test_main_menu_offers_programs_next_to_events(tmp_path: Path) -> None:
    screen = views(tmp_path).main_menu(user(images=False))

    assert button_rows(screen)[0] == ["🗓 Мероприятия", "🚀 Программы"]


def test_programs_list_filters_by_kind_and_badges_items(tmp_path: Path) -> None:
    state = CatalogState(
        CatalogFilter(
            CatalogSection.UPCOMING,
            CatalogPeriod.ALL,
            scope=CatalogScope.PROGRAMS,
            program_kind=EventKind.COURSE,
        )
    )
    item = replace(summary(7), title="Python", event_type="oc")
    listing = CatalogPage(items=(item,), page=1, pages=1, total=1, stale=False)

    screen = views(tmp_path).catalog_list(user(), state, listing)

    assert screen.text.startswith("<b>🚀 Программы</b> · 🗓 Актуальные · Курсы")
    assert "🎓 Курс · 📅" in screen.text  # noqa: RUF001
    assert button_rows(screen) == [
        ["1. Python"],
        ["Все виды", "🚀 Акселераторы"],  # noqa: RUF001
        ["🌱 Инкубаторы", "✓ 🎓 Курсы"],
        ["📦 Архив", "🏠 В меню"],  # noqa: RUF001
    ]


def test_events_list_badges_neconf(tmp_path: Path) -> None:
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))
    item = replace(summary(3), event_type="neconf")
    listing = CatalogPage(items=(item,), page=1, pages=1, total=1, stale=False)

    screen = views(tmp_path).catalog_list(user(), state, listing)

    assert "🎤 Неконференция · 📅" in screen.text


def test_program_card_shows_start_and_application_without_empty_fields(tmp_path: Path) -> None:
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING, CatalogScope.PROGRAMS))
    program = event(
        event_type="accelerator",
        address=None,
        event_languages=(),
        details_url="https://terricon.kz/kz/accelerator#form",
        registration_url="https://terricon.kz/ru/accelerator#form",
    )

    screen = views(tmp_path).event_card(
        user(posters=False), state, program, page=1, previous=None, following=None
    )

    assert "🚀 <b>Вид:</b> Акселератор" in screen.text
    assert "<b>Старт:</b>" in screen.text
    assert "Адрес" not in screen.text and "Язык" not in screen.text
    assert "Категории" not in screen.text
    links = [
        (button.text, button.url)
        for row in screen.keyboard.inline_keyboard
        for button in row
        if button.url
    ]
    assert links == [("📝 Подать заявку", "https://terricon.kz/kz/accelerator#form")]


def test_neconf_card_keeps_registration_label_and_categories(tmp_path: Path) -> None:
    state = CatalogState(CatalogFilter.default(CatalogSection.UPCOMING))
    neconf = event(
        event_type="neconf",
        address=None,
        event_languages=(),
        details_url="https://terricon.kz/ru/neconf#form",
        registration_url="https://terricon.kz/ru/neconf#form",
    )

    screen = views(tmp_path).event_card(
        user(posters=False), state, neconf, page=1, previous=None, following=None
    )

    assert "<b>Дата:</b>" in screen.text and "Категории" in screen.text
    assert "Адрес" not in screen.text
    assert any(
        button.text == "📝 Регистрация" for row in screen.keyboard.inline_keyboard for button in row
    )


def test_subscription_view_toggles_programs(tmp_path: Path) -> None:
    screen = views(tmp_path).subscriptions(user(), SubscriptionSummary(False, (), programs=True))

    assert "• 🚀 Новые наборы в программы" in screen.text
    assert "Подписки не выбраны" not in screen.text
    assert ["✓ 🚀 Новые наборы в программы"] in button_rows(screen)
