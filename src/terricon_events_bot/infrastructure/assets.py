from __future__ import annotations

from pathlib import Path, PurePosixPath

from terricon_events_bot.domain.enums import CategorySlug, Locale

SUPPORTED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


class AssetResolver:
    def __init__(self, assets_root: Path) -> None:
        self._assets_root = assets_root

    def main(self, locale: Locale) -> Path | None:
        return self.resolve(locale, "main")

    def category(self, locale: Locale, category: CategorySlug) -> Path | None:
        return self.resolve(locale, f"categories/{category.value}")

    def resolve(self, locale: Locale, relative_stem: str) -> Path | None:
        relative = PurePosixPath(relative_stem)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Asset path must remain inside the assets directory")
        for directory in (locale.value, "common"):
            stem = self._assets_root / directory / Path(*relative.parts)
            for extension in SUPPORTED_IMAGE_EXTENSIONS:
                candidate = stem.with_suffix(extension)
                if candidate.is_file():
                    return candidate
        return None
