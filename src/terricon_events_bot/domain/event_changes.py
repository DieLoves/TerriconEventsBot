from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def semantic_hash(localizations: Mapping[str, Mapping[str, str | None]]) -> str:
    payload = {
        locale: {
            "audience": value.get("audience"),
            "description": value.get("description"),
            "speaker": value.get("speaker"),
            "title": value.get("title"),
        }
        for locale, value in sorted(localizations.items())
    }
    return canonical_hash(payload)


def important_hash(snapshot: Mapping[str, object]) -> str:
    return canonical_hash(snapshot)


def structured_diff(
    old: Mapping[str, object], new: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    return {
        key: {"old": old.get(key), "new": new.get(key)}
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    }
