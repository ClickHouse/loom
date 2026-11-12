"""The per-statement ClickHouse settings a traffic class adds to its reads
(`LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)


_CLASS_SETTINGS_RAW: str | None = None
_CLASS_SETTINGS: dict[str, dict[str, Any]] = {}
# A comment body holds no `*/`, so each comment ends at its first one and a
# statement that fails to match backtracks in linear time.
_READ_HEAD = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*(?:[^*]|\*(?!/))*\*/\s*)*(SELECT|WITH)\b", re.I | re.S
)


def traffic_class_settings(traffic_class: str) -> dict[str, Any]:
    """The per-query settings `ch_settings_by_traffic_class` names for one
    class: `{class: {setting: value}}`, re-parsed only when
    the raw string changes; a malformed map is logged once and read as empty.
    '' (no class — a worker pass, a direct caller) gets nothing."""
    global _CLASS_SETTINGS_RAW, _CLASS_SETTINGS
    raw = str(settings.ch_settings_by_traffic_class)
    if raw != _CLASS_SETTINGS_RAW:
        parsed: dict[str, dict[str, Any]] = {}
        if raw.strip():
            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError("not a JSON object")
                for cls, values in obj.items():
                    if not isinstance(values, dict):
                        raise ValueError(f"{cls!r}: expected an object of setting -> value")
                    parsed[str(cls).strip().lower()] = {
                        str(k): v for k, v in values.items() if isinstance(v, (int, float, str))
                    }
            except (ValueError, TypeError) as exc:
                log.warning("LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS ignored: %s", exc)
                parsed = {}
        _CLASS_SETTINGS_RAW = raw
        _CLASS_SETTINGS = parsed
    if not traffic_class:
        return {}
    return _CLASS_SETTINGS.get(str(traffic_class).strip().lower(), {})


def is_read_statement(sql: str) -> bool:
    """True for a statement that begins with SELECT or WITH (after leading
    comments) — the shapes the class settings apply to."""
    return _READ_HEAD.match(sql or "") is not None


__all__ = ["is_read_statement", "traffic_class_settings"]
