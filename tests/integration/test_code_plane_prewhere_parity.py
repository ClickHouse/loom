"""PREWHERE and WHERE agree on a multi-part table shaped like loom.code_edges.

The code plane filters in plain WHERE because a multi-condition PREWHERE
(org AND namespace) over a multi-part `code_edges` miscounted on ClickHouse
26.5.1. This test pins that behaviour per server version, so PREWHERE can be
reconsidered on evidence.

It builds a probe table with `code_edges`' exact key and column shape in
the test database, stops merges so the inserts stay as separate parts
(the shape that miscounted), interleaves the identity keys across the
parts, and compares PREWHERE with WHERE for every identity: plain, under
FINAL, and with an IN list on the sixth key column (the `edges_for_symbols`
shape). The server version is part of every assertion message so a
failure names the build it appeared on.

Sizes are kept small for CI (twelve parts, 60k rows). `SYSTEM STOP MERGES`
is best-effort (Cloud roles lack the grant); the active-part count is in
every message, and a probe that merged to one part skips rather than
passes.
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PARTS = 12
ROWS_PER_PART = 5_000
ORGS = ["clickhouse", "dev"]
NAMESPACES = ["code-a", "code-b", "code-c", "code-d"]
COLUMNS = [
    "org",
    "namespace",
    "tier",
    "edge_kind",
    "to_symbol_id",
    "from_symbol_id",
    "path",
    "line",
    "from_qualified_name",
    "to_qualified_name",
    "confidence",
    "indexed_commit",
    "tombstoned_at",
    "version",
]


def _ddl(table: str) -> str:
    return f"""
    CREATE TABLE {table} (
      org LowCardinality(String), namespace LowCardinality(String),
      tier Enum8('treesitter' = 1, 'scip' = 2),
      edge_kind Enum8('calls' = 1, 'includes' = 2, 'inherits' = 3, 'overrides' = 4, 'uses_type' = 5, 'fixture_use' = 6),
      to_symbol_id String, from_symbol_id String, path String, line UInt32 DEFAULT 0,
      from_qualified_name String DEFAULT '', to_qualified_name String DEFAULT '',
      confidence Float32 DEFAULT 1, indexed_commit FixedString(40) DEFAULT '',
      tombstoned_at Nullable(DateTime) DEFAULT NULL, created_at DateTime DEFAULT now(),
      version UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9))),
      INDEX idx_from from_symbol_id TYPE bloom_filter GRANULARITY 4,
      INDEX idx_path path TYPE bloom_filter(0.01) GRANULARITY 4
    ) ENGINE = ReplacingMergeTree(version)
    ORDER BY (org, namespace, tier, to_symbol_id, edge_kind, from_symbol_id, path, line)
    SETTINGS index_granularity = 8192
    """


def _rows(part: int, rng: random.Random) -> list[list[Any]]:
    out: list[list[Any]] = []
    for i in range(ROWS_PER_PART):
        org = ORGS[(i + part) % 2 if i % 7 else 0]
        ns = NAMESPACES[(i * 31 + part) % 4]
        out.append(
            [
                org,
                ns,
                1 if i % 5 else 2,
                1 if i % 9 else 3,
                f"sym{rng.randrange(20_000):05d}",
                f"sym{rng.randrange(20_000):05d}",
                f"src/{(i * 13) % 200}/f{(i * 7) % 900}.cpp",
                i % 5000,
                "a::b",
                "c::d",
                1.0 if i % 3 else 0.6,
                "x" * 40,
                None,
                part * 100_000 + i,
            ]
        )
    return out


def _count(ch: Any, sql: str, params: dict[str, Any]) -> int:
    return int(ch.query(sql, parameters=params).result_rows[0][0])


def test_prewhere_matches_where_on_a_multi_part_code_edges_shape(ch_client: Any) -> None:
    version = str(ch_client.query("SELECT version()").result_rows[0][0])
    table = f"loom.prewhere_parity_probe_{os.urandom(3).hex()}"
    ch_client.command(_ddl(table))
    try:
        try:
            ch_client.command(f"SYSTEM STOP MERGES {table}")
        except Exception:  # noqa: BLE001 - Cloud roles lack SYSTEM MERGES; the parts count says what held
            pass
        rng = random.Random(7)
        for part in range(PARTS):
            ch_client.insert(table, _rows(part, rng), column_names=COLUMNS)
        parts = _count(
            ch_client,
            "SELECT count() FROM system.parts WHERE database = 'loom' AND table = {t:String} AND active",
            {"t": table.split(".", 1)[1]},
        )
        if parts < 2:
            pytest.skip(f"probe merged to one part before the read on {version}; shape not held")

        mismatches: list[str] = []
        for org in ORGS:
            for ns in NAMESPACES:
                p = {"o": org, "n": ns}
                ids = [f"sym{rng.randrange(20_000):05d}" for _ in range(50)]
                pairs = {
                    "plain": (
                        f"SELECT count() FROM {table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                        f"SELECT count() FROM {table} PREWHERE org = {{o:String}} AND namespace = {{n:String}}",
                        p,
                    ),
                    "final": (
                        f"SELECT count() FROM {table} FINAL WHERE org = {{o:String}} AND namespace = {{n:String}}",
                        f"SELECT count() FROM {table} FINAL PREWHERE org = {{o:String}} AND namespace = {{n:String}}",
                        p,
                    ),
                    "in_list": (
                        f"SELECT count() FROM {table} WHERE org = {{o:String}} AND namespace = {{n:String}}"
                        " AND from_symbol_id IN {ids:Array(String)}",
                        f"SELECT count() FROM {table} PREWHERE org = {{o:String}} AND namespace = {{n:String}}"
                        " WHERE from_symbol_id IN {ids:Array(String)}",
                        {**p, "ids": ids},
                    ),
                }
                for shape, (where_sql, prewhere_sql, params) in pairs.items():
                    w, pw = (
                        _count(ch_client, where_sql, params),
                        _count(ch_client, prewhere_sql, params),
                    )
                    if w != pw:
                        mismatches.append(f"{org}/{ns} {shape}: WHERE={w} PREWHERE={pw}")
        assert not mismatches, (
            f"PREWHERE disagreed with WHERE on ClickHouse {version} over {parts} parts: {mismatches}"
        )
    finally:
        ch_client.command(f"DROP TABLE IF EXISTS {table}")
