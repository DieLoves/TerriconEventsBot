from pathlib import Path

import pytest

from terricon_events_bot.domain.enums import Locale
from terricon_events_bot.localization import LocalizationCatalog, LocalizationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_project_locales_have_identical_keys_and_ru_fallback() -> None:
    catalog = LocalizationCatalog.load(PROJECT_ROOT / "locales")

    assert catalog.get(Locale.RU, "menu.events") == "Мероприятия"
    assert catalog.get(Locale.KZ, "menu.events") == "Мероприятия"
    assert "menu.events" in catalog.unresolved_todos()


def test_public_kz_is_rejected_while_todos_remain() -> None:
    catalog = LocalizationCatalog.load(PROJECT_ROOT / "locales")

    with pytest.raises(LocalizationError, match="Public KZ mode is unavailable"):
        catalog.ensure_public_kz_ready()


def test_complete_kz_catalog_can_be_public(tmp_path: Path) -> None:
    (tmp_path / "ru.yaml").write_text('key: "Русский"\n', encoding="utf-8")
    (tmp_path / "kz.yaml").write_text('key: "Қазақша"\n', encoding="utf-8")
    catalog = LocalizationCatalog.load(tmp_path)

    catalog.ensure_public_kz_ready()

    assert catalog.get(Locale.KZ, "key") == "Қазақша"


def test_locale_key_mismatch_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "ru.yaml").write_text('first: "1"\nsecond: "2"\n', encoding="utf-8")
    (tmp_path / "kz.yaml").write_text('first: "1"\nextra: "3"\n', encoding="utf-8")

    with pytest.raises(LocalizationError, match="keys differ"):
        LocalizationCatalog.load(tmp_path)
