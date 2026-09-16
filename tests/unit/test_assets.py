from pathlib import Path

import pytest

from terricon_events_bot.domain.enums import CategorySlug, Locale
from terricon_events_bot.infrastructure.assets import AssetResolver


def create_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test image placeholder")
    return path


def test_localized_asset_takes_priority_over_common(tmp_path: Path) -> None:
    common = create_image(tmp_path / "common" / "main.png")
    localized = create_image(tmp_path / "ru" / "main.webp")
    resolver = AssetResolver(tmp_path)

    assert resolver.main(Locale.RU) == localized
    assert resolver.main(Locale.KZ) == common


def test_category_asset_and_missing_asset(tmp_path: Path) -> None:
    category = create_image(tmp_path / "common" / "categories" / "ai_data.jpg")
    resolver = AssetResolver(tmp_path)

    assert resolver.category(Locale.KZ, CategorySlug.AI_DATA) == category
    assert resolver.category(Locale.RU, CategorySlug.OTHER) is None


def test_asset_resolver_rejects_path_traversal(tmp_path: Path) -> None:
    resolver = AssetResolver(tmp_path)

    with pytest.raises(ValueError, match="inside"):
        resolver.resolve(Locale.RU, "../secret")
