import pytest
from pydantic import ValidationError

from terricon_events_bot.domain.catalog import (
    CatalogPeriod,
    CatalogSection,
    EventLanguage,
)
from terricon_events_bot.domain.enums import (
    BroadcastAudience,
    CategorySlug,
    EventFormat,
    FeedbackKind,
    Locale,
)
from terricon_events_bot.telegram.callbacks import (
    AdminTicketAction,
    AdminTicketCallback,
    BroadcastAction,
    BroadcastActionCallback,
    BroadcastAudienceCallback,
    CatalogCategoryCallback,
    CatalogFormatCallback,
    CatalogLanguageCallback,
    CatalogPageCallback,
    CatalogPeriodCallback,
    ChooseLocaleCallback,
    EventAction,
    EventCallback,
    FeedbackAction,
    FeedbackActionCallback,
    FeedbackKindCallback,
    SubscriptionAllCallback,
    SubscriptionCategoryCallback,
)


def test_callback_contracts_round_trip_stable_codes_and_fit_telegram_limit() -> None:
    values = (
        ChooseLocaleCallback(locale=Locale.KZ),
        CatalogCategoryCallback(
            section=CatalogSection.UPCOMING,
            category=CategorySlug.SOFT_SKILLS_LANGUAGES,
        ),
        CatalogPeriodCallback(period=CatalogPeriod.MONTHS_3),
        CatalogFormatCallback(event_format=EventFormat.HYBRID),
        CatalogLanguageCallback(language=EventLanguage.EN),
        CatalogPageCallback(page=123),
        EventCallback(
            event_id=9_223_372_036_854_775_807,
            action=EventAction.DESCRIPTION,
            list_page=99,
            content_page=99,
        ),
        SubscriptionAllCallback(),
        SubscriptionCategoryCallback(category=CategorySlug.SOFT_SKILLS_LANGUAGES),
        FeedbackKindCallback(kind=FeedbackKind.ERROR),
        FeedbackActionCallback(action=FeedbackAction.CONFIRM),
        AdminTicketCallback(
            ticket_id=9_223_372_036_854_775_807,
            action=AdminTicketAction.UNBLOCK,
        ),
        BroadcastAudienceCallback(audience=BroadcastAudience.BOTH),
        BroadcastActionCallback(
            broadcast_id=9_223_372_036_854_775_807,
            action=BroadcastAction.CONFIRM,
        ),
    )

    for callback in values:
        packed = callback.pack()
        assert len(packed.encode()) <= 64
        assert type(callback).unpack(packed) == callback


def test_callback_contracts_reject_user_text_and_wrong_prefix() -> None:
    with pytest.raises(ValidationError):
        CatalogPageCallback(page="user text")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="prefix"):
        CatalogPageCallback.unpack("other:1")
