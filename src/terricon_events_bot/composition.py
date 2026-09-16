from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from terricon_events_bot.config import Settings
from terricon_events_bot.domain.enums import AccessMode
from terricon_events_bot.infrastructure.assets import AssetResolver
from terricon_events_bot.infrastructure.database import create_engine, create_session_factory
from terricon_events_bot.localization import LocalizationCatalog


@dataclass(frozen=True, slots=True)
class Foundation:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    localizations: LocalizationCatalog
    assets: AssetResolver


def build_foundation(settings: Settings, project_root: Path) -> Foundation:
    localizations = LocalizationCatalog.load(project_root / "locales")
    if settings.access_mode is AccessMode.PUBLIC:
        localizations.ensure_public_kz_ready()
    engine = create_engine(settings.database_url.get_secret_value())
    return Foundation(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        localizations=localizations,
        assets=AssetResolver(project_root / "assets"),
    )
