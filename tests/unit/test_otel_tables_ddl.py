"""schemas/otel/clickstack_otel_tables.sql creates every table Loom's OTel
exporters insert into and the erasure cascade deletes from, each with the
index that cascade reads by."""

from __future__ import annotations

import re
from pathlib import Path

from server.erasure.store.health import OTEL_METRICS_TABLES

ROOT = Path(__file__).resolve().parents[2]
DDL = (ROOT / "schemas" / "otel" / "clickstack_otel_tables.sql").read_text()
_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS default\.(\w+) \((.*?)\n\)\nENGINE", re.S)
TABLES = {m.group(1): m.group(2) for m in _CREATE.finditer(DDL)}


def test_every_exported_table_is_created() -> None:
    src = (ROOT / "server" / "observability" / "otel_exporters.py").read_text()
    inserted = set(re.findall(r'"default\.(otel_\w+)"', src))
    assert inserted, "no exporter target found in otel_exporters.py"
    assert inserted <= set(TABLES)
    assert set(OTEL_METRICS_TABLES) <= set(TABLES)


def test_every_metrics_table_carries_the_erasure_index() -> None:
    for name in OTEL_METRICS_TABLES:
        assert re.search(
            r"INDEX idx_attr_value\s+mapValues\(Attributes\)\s+TYPE bloom_filter", TABLES[name]
        ), name


def test_every_statement_is_rerunnable() -> None:
    creates = re.findall(r"^CREATE\b.*$", DDL, re.M)
    assert creates and all(c.startswith("CREATE TABLE IF NOT EXISTS default.") for c in creates)
