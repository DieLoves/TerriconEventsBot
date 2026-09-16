from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from terricon_events_bot.domain.enums import Locale


class LocalizationError(ValueError):
    pass


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flattened: dict[str, str] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten(value, path))
        elif isinstance(value, str):
            flattened[path] = value
        else:
            raise LocalizationError(f"Localization value {path!r} must be a string")
    return flattened


def _read_locale(path: Path) -> dict[str, str]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise LocalizationError(f"Cannot load locale file: {path}") from error
    if not isinstance(raw, dict):
        raise LocalizationError(f"Locale file must contain a mapping: {path}")
    return _flatten(raw)


class LocalizationCatalog:
    def __init__(self, values: dict[Locale, dict[str, str]]) -> None:
        self._values = values
        ru_keys = set(values[Locale.RU])
        for locale, localized in values.items():
            if set(localized) != ru_keys:
                missing = sorted(ru_keys - set(localized))
                extra = sorted(set(localized) - ru_keys)
                raise LocalizationError(
                    f"Locale {locale.value} keys differ; missing={missing}, extra={extra}"
                )

    @classmethod
    def load(cls, directory: Path) -> LocalizationCatalog:
        return cls(
            {
                Locale.RU: _read_locale(directory / "ru.yaml"),
                Locale.KZ: _read_locale(directory / "kz.yaml"),
            }
        )

    def get(self, locale: Locale, key: str) -> str:
        try:
            value = self._values[locale][key]
        except KeyError as error:
            raise LocalizationError(f"Unknown localization key: {key}") from error
        if locale is Locale.KZ and self.is_todo(value):
            return self._values[Locale.RU][key]
        return value

    def unresolved_todos(self, locale: Locale = Locale.KZ) -> tuple[str, ...]:
        return tuple(key for key, value in self._values[locale].items() if self.is_todo(value))

    def ensure_public_kz_ready(self) -> None:
        unresolved = self.unresolved_todos(Locale.KZ)
        if unresolved:
            raise LocalizationError(
                f"Public KZ mode is unavailable: {len(unresolved)} TODO strings remain"
            )

    @staticmethod
    def is_todo(value: str) -> bool:
        return not value.strip() or value.lstrip().startswith("TODO")
