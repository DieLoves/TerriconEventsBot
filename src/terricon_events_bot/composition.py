from dataclasses import dataclass
from pathlib import Path

import httpx
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.application.classification import (
    ClassificationWorker,
    OpenAIPricing,
)
from terricon_events_bot.application.sync import SyncService
from terricon_events_bot.config import Settings
from terricon_events_bot.domain.enums import AccessMode
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.infrastructure.openai_adapter import OpenAIEventAdapter
from terricon_events_bot.infrastructure.terricon import TerriconClient
from terricon_events_bot.localization import LocalizationCatalog


@dataclass(frozen=True, slots=True)
class Foundation:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    localizations: LocalizationCatalog
    assets: AssetResolver
    http_client: httpx.AsyncClient
    terricon_client: TerriconClient
    sync_service: SyncService
    openai_client: AsyncOpenAI
    openai_adapter: OpenAIEventAdapter
    classification_worker: ClassificationWorker

    async def close(self) -> None:
        try:
            await self.http_client.aclose()
        finally:
            try:
                await self.openai_client.close()
            finally:
                await self.engine.dispose()


def build_foundation(settings: Settings, project_root: Path) -> Foundation:
    localizations = LocalizationCatalog.load(project_root / "locales")
    if settings.access_mode is AccessMode.PUBLIC:
        localizations.ensure_public_kz_ready()
    engine = create_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)
    http_client = httpx.AsyncClient()
    terricon_client = TerriconClient(
        str(settings.base_url),
        http_client,
        max_retries=settings.sync_max_retries,
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
    return Foundation(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        localizations=localizations,
        assets=AssetResolver(project_root / "assets"),
        http_client=http_client,
        terricon_client=terricon_client,
        sync_service=SyncService(session_factory, terricon_client),
        openai_client=openai_client,
        openai_adapter=openai_adapter,
        classification_worker=classification_worker,
    )
