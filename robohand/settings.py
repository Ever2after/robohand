"""User settings persisted as DATA_ROOT/settings.json (device serials, ranges, UI choices)."""
from __future__ import annotations

import json

from .paths import SETTINGS


def read_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_settings(data):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(SETTINGS)
