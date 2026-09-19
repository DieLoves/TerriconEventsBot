from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData

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


class NavigationView(StrEnum):
    MENU = "m"
    EVENTS = "e"
    FILTERS = "f"
    SETTINGS = "s"
    SUBSCRIPTIONS = "b"
    FEEDBACK = "x"
    ABOUT = "a"
    PRIVACY = "p"
    PRIVACY_WARNING = "d"


class EventAction(StrEnum):
    CARD = "c"
    DESCRIPTION = "d"


class ToggleSetting(StrEnum):
    MENU_IMAGES = "m"
    EVENT_POSTERS = "p"


class FeedbackAction(StrEnum):
    SKIP_PHOTO = "s"
    CONFIRM = "y"
    CANCEL = "x"


class AdminTicketAction(StrEnum):
    VIEW = "v"
    REPLY = "r"
    CLOSE = "c"
    BLOCK = "b"
    UNBLOCK = "u"


class AdminAction(StrEnum):
    MENU = "m"
    STATUS = "s"
    STATS = "t"
    SYNC = "y"
    TICKETS = "k"
    BROADCAST = "b"


class BroadcastAction(StrEnum):
    SKIP_PHOTO = "s"
    CONFIRM = "y"
    CANCEL = "x"


class ChooseLocaleCallback(CallbackData, prefix="ol"):
    locale: Locale


class ContinueOnboardingCallback(CallbackData, prefix="oc"):
    pass


class NavigationCallback(CallbackData, prefix="n"):
    view: NavigationView


class CatalogSectionCallback(CallbackData, prefix="cs"):
    section: CatalogSection


class CatalogCategoryCallback(CallbackData, prefix="cc"):
    section: CatalogSection
    category: CategorySlug


class CatalogAllCallback(CallbackData, prefix="ca"):
    section: CatalogSection


class CatalogPeriodCallback(CallbackData, prefix="cp"):
    period: CatalogPeriod


class CatalogFormatCallback(CallbackData, prefix="cf"):
    event_format: EventFormat


class CatalogFormatAllCallback(CallbackData, prefix="cfa"):
    pass


class CatalogLanguageCallback(CallbackData, prefix="cl"):
    language: EventLanguage


class CatalogLanguageAllCallback(CallbackData, prefix="cla"):
    pass


class CatalogPageCallback(CallbackData, prefix="pg"):
    page: int


class EventCallback(CallbackData, prefix="ev"):
    event_id: int
    action: EventAction
    list_page: int = 1
    content_page: int = 1


class SettingLocaleCallback(CallbackData, prefix="sl"):
    locale: Locale


class SettingToggleCallback(CallbackData, prefix="st"):
    setting: ToggleSetting


class SubscriptionAllCallback(CallbackData, prefix="sa"):
    pass


class SubscriptionCategoryCallback(CallbackData, prefix="sb"):
    category: CategorySlug


class FeedbackKindCallback(CallbackData, prefix="fk"):
    kind: FeedbackKind


class FeedbackActionCallback(CallbackData, prefix="fa"):
    action: FeedbackAction


class DeleteProfileCallback(CallbackData, prefix="du"):
    pass


class AdminActionCallback(CallbackData, prefix="aa"):
    action: AdminAction


class AdminTicketCallback(CallbackData, prefix="at"):
    ticket_id: int
    action: AdminTicketAction


class BroadcastAudienceCallback(CallbackData, prefix="ba"):
    audience: BroadcastAudience


class BroadcastActionCallback(CallbackData, prefix="bx"):
    broadcast_id: int
    action: BroadcastAction
