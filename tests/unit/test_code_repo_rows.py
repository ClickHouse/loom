"""Row-builder contract for the code plane (mirror-every-column GATE).

Every loom.code_* writer goes through build_row()/tombstone_of() — these
tests pin that (a) a built row carries every persisted column, (b) unknown
columns raise, (c) a tombstone mirrors the live row rather than zeroing
fields under FINAL.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from server.code.store.index import CodeRepo, build_row
from server.code.store.index.rows import _TABLE_COLS


@pytest.mark.parametrize("table", sorted(_TABLE_COLS))
def test_built_row_carries_every_column(table: str) -> None:
    row = build_row(table)
    assert set(row) == set(_TABLE_COLS[table])
    assert row["version"] > 0
    # every table stamps its rows: created_at, or computed_at for the
    # weekly-rewritten stats tables (code_symbol_stats)
    assert row.get("created_at", row.get("computed_at")) is not None


@pytest.mark.parametrize("table", sorted(_TABLE_COLS))
def test_registry_equals_the_schema_columns(table: str) -> None:
    """A column the DDL has and the registry lacks is dropped by every
    tombstone_of()/moved_of() mirror, so FINAL readers see its default."""
    text = (Path(__file__).resolve().parents[2] / "schemas" / "schema.sql").read_text()
    m = re.search(
        rf"CREATE TABLE IF NOT EXISTS loom\.{re.escape(table)}\n\((.*?)\n\)\nENGINE", text, re.S
    )
    assert m, table
    ddl = {
        name
        for name, rest in re.findall(r"(?m)^\s{4}`(\w+)`(.*)$", m.group(1))
        if " MATERIALIZED " not in rest and " ALIAS " not in rest
    }
    assert set(_TABLE_COLS[table]) == ddl, set(_TABLE_COLS[table]) ^ ddl


def test_unknown_column_raises() -> None:
    with pytest.raises(ValueError, match="unknown columns"):
        build_row("code_symbols", qualified_nam="typo")


def test_list_defaults_not_shared() -> None:
    a = build_row("code_chunks")
    b = build_row("code_chunks")
    a["symbol_ids"].append("x")
    assert b["symbol_ids"] == []


def test_tombstone_mirrors_live_row() -> None:
    live = build_row(
        "code_symbols",
        org="o",
        namespace="n",
        tier="scip",
        qualified_name="DB::f",
        symbol_id="abc",
        name="f",
        kind="function",
        signature="(int)",
        path="src/f.cpp",
        start_line=10,
        end_line=20,
        doc_comment="/// doc",
        indexed_commit="c" * 40,
    )
    tomb = CodeRepo.tombstone_of("code_symbols", live)
    assert tomb["tombstoned_at"] is not None
    assert tomb["version"] > live["version"]
    for col in _TABLE_COLS["code_symbols"]:
        if col in {"tombstoned_at", "version"}:
            continue
        assert tomb[col] == live[col], f"tombstone dropped {col}"


def test_versions_monotonic() -> None:
    rows = [build_row("code_files") for _ in range(3)]
    versions = [r["version"] for r in rows]
    assert versions == sorted(versions)
    assert len(set(versions)) == 3


def _row(
    tier: str, qn: str, sid: str, start: int, end: int, path: str = "src/Functions/readWkt.cpp"
) -> dict:
    return {
        "tier": tier,
        "qualified_name": qn,
        "name": qn.rsplit("::", 1)[-1],
        "symbol_id": sid,
        "path": path,
        "start_line": start,
        "end_line": end,
        "signature": "()",
    }


def test_prefer_scip_collapses_file_local_definition_across_tiers() -> None:
    from server.code.store.index.symbol_rank import _prefer_scip

    scip = _row(
        "scip",
        "DB::$anonymous_namespace_src/Functions/readWkt.cpp::isEmptyPointWKT",
        "f7b8",
        33,
        33,
    )
    ts = _row("treesitter", "DB::isEmptyPointWKT", "b123", 33, 53)
    out = _prefer_scip([scip, ts])
    assert [r["tier"] for r in out] == ["scip"]
    assert out[0]["end_line"] == 53  # parser span carried onto the compiler row


def test_prefer_scip_keeps_distinct_definitions() -> None:
    from server.code.store.index.symbol_rank import _prefer_scip

    a = _row("scip", "DB::$anonymous_namespace_src/A.cpp::helper", "1", 10, 10, path="src/A.cpp")
    b = _row("treesitter", "DB::helper", "2", 40, 60, path="src/B.cpp")  # other file
    c = _row("treesitter", "DB::helper", "3", 100, 120, path="src/A.cpp")  # same file, other site
    assert len(_prefer_scip([a, b, c])) == 3


def test_prefer_scip_keeps_the_parser_extent_of_a_single_line_compiler_row() -> None:
    """scip records a class (or a function it saw only declared) as one
    line; the parser row for the same symbol carries the body. The
    surviving compiler row must inherit that extent, else a grep hit inside
    a 1,400-line class reports the class as 773–773 (live: Context.h)."""
    from server.code.store.index.symbol_rank import _prefer_scip

    scip = {
        "symbol_id": "s",
        "qualified_name": "DB::Context",
        "name": "Context",
        "path": "src/Context.h",
        "start_line": 773,
        "end_line": 773,
        "tier": "scip",
        "kind": "class",
    }
    ts = {
        "symbol_id": "s",
        "qualified_name": "DB::Context",
        "name": "Context",
        "path": "src/Context.h",
        "start_line": 773,
        "end_line": 2186,
        "tier": "treesitter",
        "kind": "class",
    }
    for order in ([scip, dict(ts)], [dict(ts), dict(scip)]):
        out = _prefer_scip([dict(r) for r in order])
        assert len(out) == 1 and out[0]["tier"] == "scip"
        assert (out[0]["start_line"], out[0]["end_line"]) == (773, 2186)
    # a compiler row WITH its own extent is never widened by the parser
    real = dict(scip, end_line=900)
    out = _prefer_scip([real, dict(ts)])
    assert (out[0]["start_line"], out[0]["end_line"]) == (773, 900)
