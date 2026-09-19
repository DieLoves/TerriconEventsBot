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
    assert len(buttons) == len(CategorySlug) + 2
    assert "✓ AI и Data" in buttons
    assert "✓ Другое" in buttons
    assert "Все новые мероприятия" in buttons  # noqa: RUF001


def test_kz_fallback_and_image_preferences_are_applied(tmp_path: Path) -> None:
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

    assert telegram_views.main_menu(kz_user).text == "<b>Главное меню</b>"
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

    feedback = telegram_views.feedback(current_user)
    preview = telegram_views.feedback_preview(
        current_user,
        FeedbackDraft(FeedbackKind.ERROR, "Unsafe <text>", "photo-id", "preview"),
    )
    privacy = telegram_views.privacy(current_user, warning=True)

    assert {button.text for row in feedback.keyboard.inline_keyboard for button in row} >= {
        "Ошибка",
        "Идея",
        "Другое",
    }
    assert "Unsafe &lt;text&gt;" in preview.text
    assert "Подтвердить" in {
        button.text for row in privacy.keyboard.inline_keyboard for button in row
    }
