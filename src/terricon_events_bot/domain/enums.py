from enum import StrEnum


class AccessMode(StrEnum):
    ALLOWLIST = "allowlist"
    PUBLIC = "public"


class Locale(StrEnum):
    RU = "ru"
    KZ = "kz"


class SourceTheme(StrEnum):
    IT = "it"
    BUSINESS = "business"
    MARKETING = "marketing"


class CategorySlug(StrEnum):
    DEVELOPMENT_IT = "development_it"
    AI_DATA = "ai_data"
    BUSINESS_FINANCE = "business_finance"
    MARKETING_SALES = "marketing_sales"
    DESIGN_CREATIVE = "design_creative"
    STARTUPS_PRODUCTS = "startups_products"
    CAREER_EDUCATION_HR = "career_education_hr"
    SOFT_SKILLS_LANGUAGES = "soft_skills_languages"
    OTHER = "other"


class EventFormat(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class EventState(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"
    CANCELLED = "cancelled"


class NotificationType(StrEnum):
    NEW_EVENT = "new_event"
    IMPORTANT_CHANGE = "important_change"
    CANCELLATION = "cancellation"


class TicketStatus(StrEnum):
    OPEN = "open"
    AWAITING_ADMIN = "awaiting_admin"
    AWAITING_USER = "awaiting_user"
    CLOSED = "closed"


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
