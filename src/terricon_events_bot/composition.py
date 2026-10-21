from dataclasses import dataclass
from pathlib import Path

import httpx
from aiogram import Bot, Router
from groq import AsyncGroq
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.admin import AdminService
from terricon_events_bot.application.broadcasts import BroadcastService, BroadcastWorker
from terricon_events_bot.application.catalog import CatalogQuery, CatalogStateStore
from terricon_events_bot.application.classification import (
    AIPricing,
    ClassificationWorker,
)
from terricon_events_bot.application.delivery import (
    DeliveryWorker,
    DigestRenderer,
    OutboxService,
    QuietHours,
)
from terricon_events_bot.application.feedback import FeedbackService
from terricon_events_bot.application.operations import (
    AdvisoryJobCoordinator,
    LockedSyncRunner,
    SyncRunner,
)
from terricon_events_bot.application.subscriptions import SubscriptionService
from terricon_events_bot.application.sync import SyncService
from terricon_events_bot.application.users import UserService
from terricon_events_bot.config import Settings
from terricon_events_bot.domain.enums import AccessMode
from terricon_events_bot.infrastructure.ai_quota import PostgresAIQuota, QuotaLimits
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.groq_adapter import GroqEventAdapter
from terricon_events_bot.infrastructure.terricon import TerriconClient
from terricon_events_bot.localization import LocalizationCatalog
from terricon_events_bot.telegram.admin import AdminTelegramController, build_admin_router
from terricon_events_bot.telegram.broadcasts import AiogramBroadcastGateway
from terricon_events_bot.telegram.delivery import AiogramTelegramGateway
from terricon_events_bot.telegram.feedback import (
    AiogramFeedbackGateway,
    FeedbackTelegramController,
)
from terricon_events_bot.telegram.middleware import UserContextMiddleware
from terricon_events_bot.telegram.rendering import ScreenRenderer
from terricon_events_bot.telegram.router import build_user_router
from terricon_events_bot.telegram.views import TelegramViews


@dataclass(frozen=True, slots=True)
class Foundation:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    localizations: LocalizationCatalog
    assets: AssetResolver
    http_client: httpx.AsyncClient
    terricon_client: TerriconClient
    sync_service: SyncRunner
    job_coordinator: AdvisoryJobCoordinator
    groq_client: AsyncGroq
    groq_adapter: GroqEventAdapter
    classification_worker: ClassificationWorker
    outbox_service: OutboxService
    delivery_worker: DeliveryWorker
    feedback_service: FeedbackService
    admin_service: AdminService
    broadcast_service: BroadcastService
    broadcast_worker: BroadcastWorker
    bot: Bot
    catalog_query: CatalogQuery
    catalog_state: CatalogStateStore
    user_service: UserService
    subscription_service: SubscriptionService
    screen_renderer: ScreenRenderer
    telegram_views: TelegramViews
    telegram_router: Router

    async def close(self) -> None:
        try:
            await self.http_client.aclose()
        finally:
            try:
                await self.groq_client.close()
            finally:
                try:
                    await self.bot.session.close()
                finally:
                    await self.engine.dispose()


def build_foundation(settings: Settings, project_root: Path) -> Foundation:
    localizations = LocalizationCatalog.load(project_root / "locales")
    if settings.access_mode is AccessMode.PUBLIC:
        localizations.ensure_public_kz_ready()
    assets = AssetResolver(project_root / "assets")
    engine = create_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)
    http_client = httpx.AsyncClient()
    terricon_client = TerriconClient(
        str(settings.base_url),
        http_client,
        max_retries=settings.sync_max_retries,
    )
    job_coordinator = AdvisoryJobCoordinator(session_factory)
    sync_service = LockedSyncRunner(
        SyncService(session_factory, terricon_client),
        job_coordinator,
    )
    groq_client = AsyncGroq(
        api_key=settings.groq_api_key.get_secret_value(),
        timeout=settings.groq_timeout_seconds,
        max_retries=0,
    )
    groq_adapter = GroqEventAdapter(
        groq_client,
        settings.groq_model,
        quota=PostgresAIQuota(
            session_factory,
            settings.groq_quota_scope,
            QuotaLimits(settings.groq_rpm, settings.groq_rpd, settings.groq_tpm, settings.groq_tpd),
        ),
        max_reserved_tokens=min(settings.groq_tpm, settings.groq_tpd),
    )
    classification_worker = ClassificationWorker(
        session_factory,
        groq_adapter,
        model=settings.groq_model,
        monthly_budget_usd=settings.groq_monthly_budget_usd,
        pricing=AIPricing(
            input_per_million_usd=settings.groq_input_price_per_million_usd,
            output_per_million_usd=settings.groq_output_price_per_million_usd,
        ),
    )
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    digest_renderer = DigestRenderer(localizations, timezone=settings.timezone)
    outbox_service = OutboxService(
        session_factory,
        digest_renderer,
        QuietHours(
            settings.timezone,
            settings.quiet_hours_start,
            settings.quiet_hours_end,
        ),
    )
    delivery_worker = DeliveryWorker(
        session_factory,
        AiogramTelegramGateway(bot),
    )
    catalog_query = CatalogQuery(
        session_factory,
        timezone=settings.timezone,
        page_size=settings.events_page_size,
    )
    catalog_state = CatalogStateStore(session_factory)
    user_service = UserService(
        session_factory,
        access_mode=settings.access_mode,
        allowed_telegram_ids=settings.allowed_telegram_ids,
        admin_telegram_ids=settings.admin_telegram_ids,
    )
    subscription_service = SubscriptionService(session_factory)
    screen_renderer = ScreenRenderer(bot, session_factory)
    telegram_views = TelegramViews(
        localizations,
        assets,
        timezone=settings.timezone,
    )
    middleware = UserContextMiddleware(user_service, localizations)
    feedback_service = FeedbackService(session_factory)
    admin_service = AdminService(session_factory)
    broadcast_service = BroadcastService(session_factory)
    broadcast_worker = BroadcastWorker(
        session_factory,
        AiogramBroadcastGateway(bot),
    )
    feedback_gateway = AiogramFeedbackGateway(
        bot,
        settings.resolved_admin_chat_id,
        localizations,
    )
    feedback_controller = FeedbackTelegramController(
        feedback_service,
        feedback_gateway,
        user_service,
        screen_renderer,
        telegram_views,
    )
    user_router = build_user_router(
        user_service,
        subscription_service,
        catalog_query,
        catalog_state,
        screen_renderer,
        telegram_views,
        feedback=feedback_controller,
    )
    admin_router = build_admin_router(
        AdminTelegramController(
            admin_service,
            feedback_service,
            broadcast_service,
            sync_service,
            feedback_gateway,
            user_service,
        ),
    )
    # Registered once on the parent: sibling routers would otherwise each resolve
    # (and upsert) the user for every update that falls through the admin router.
    telegram_router = Router(name="application")
    telegram_router.message.outer_middleware(middleware)
    telegram_router.callback_query.outer_middleware(middleware)
    telegram_router.include_router(admin_router)
    telegram_router.include_router(user_router)
    return Foundation(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        localizations=localizations,
        assets=assets,
        http_client=http_client,
        terricon_client=terricon_client,
        sync_service=sync_service,
        job_coordinator=job_coordinator,
        groq_client=groq_client,
        groq_adapter=groq_adapter,
        classification_worker=classification_worker,
        outbox_service=outbox_service,
        delivery_worker=delivery_worker,
        feedback_service=feedback_service,
        admin_service=admin_service,
        broadcast_service=broadcast_service,
        broadcast_worker=broadcast_worker,
        bot=bot,
        catalog_query=catalog_query,
        catalog_state=catalog_state,
        user_service=user_service,
        subscription_service=subscription_service,
        screen_renderer=screen_renderer,
        telegram_views=telegram_views,
        telegram_router=telegram_router,
    )
