"""deploy/dashboards/code_plane.json is the source a HyperDX board is rebuilt
from: every tile has a unique id, a known source and a time-bounded query
(an unbounded tile scans the whole OTel table on every refresh), and every
alert states its condition and its reason."""

from __future__ import annotations

import json
import re
from pathlib import Path

SPEC = Path(__file__).resolve().parents[2] / "deploy" / "dashboards" / "code_plane.json"
_TIME_BOUND = re.compile(
    r"\b(?:\w+\.)?(?:TimeUnix|Timestamp|ts)\s*>\s*now\(\)\s*-\s*INTERVAL\s+\d+\s+(?:MINUTE|HOUR|DAY)\b"
)
_TABLES = {
    "metrics": re.compile(r"\bFROM default\.otel_metrics_(?:gauge|sum|histogram)\b"),
    "traces": re.compile(r"\bFROM default\.otel_traces\b"),
    "sql": re.compile(r"\bFROM loom\.\w+"),
}


def _tiles() -> list[dict]:
    return json.loads(SPEC.read_text(encoding="utf-8"))["tiles"]


def test_tile_ids_are_unique() -> None:
    ids = [t["id"] for t in _tiles()]
    assert len(ids) == len(set(ids))


def test_every_tile_reads_its_declared_source_within_a_time_window() -> None:
    for t in _tiles():
        assert t["source"] in _TABLES, t["id"]
        assert _TABLES[t["source"]].search(t["sql"]), t["id"]
        assert _TIME_BOUND.search(t["sql"]), f"{t['id']}: no time bound"


def test_every_alert_names_its_condition_and_reason() -> None:
    alerts = [t["alert"] for t in _tiles() if "alert" in t]
    assert alerts
    for a in alerts:
        assert a["name"] and a["condition"] and a["why"], a
