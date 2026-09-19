from dataclasses import dataclass
from pathlib import Path

import httpx
from aiogram import Bot, Router
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.admin import AdminService
from terricon_events_bot.application.broadcasts import BroadcastService, BroadcastWorker
from terricon_events_bot.application.catalog import CatalogQuery, CatalogStateStore
from terricon_events_bot.application.classification import (
    ClassificationWorker,
    OpenAIPricing,
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
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.openai_adapter import OpenAIEventAdapter
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
    openai_client: AsyncOpenAI
    openai_adapter: OpenAIEventAdapter
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
                await self.openai_client.close()
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
    openai_client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=0,
    )
    openai_adapter = OpenAIEventAdapter(openai_client, settings.openai_model)
    classification_worker = ClassificationWorker(
        session_factory,
        openai_adapter,
        model=settings.openai_model,
        monthly_budget_usd=settings.openai_monthly_budget_usd,
        pricing=OpenAIPricing(
            input_per_million_usd=settings.openai_input_price_per_million_usd,
            output_per_million_usd=settings.openai_output_price_per_million_usd,
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
    feedback_gateway = AiogramFeedbackGateway(bot, settings.resolved_admin_chat_id)
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
        middleware,
        feedback_controller,
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
        middleware,
    )
    telegram_router = Router(name="application")
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
        openai_client=openai_client,
        openai_adapter=openai_adapter,
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
