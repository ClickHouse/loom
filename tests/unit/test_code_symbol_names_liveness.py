"""Edge resolution reads the name-keyed side table alone.

`code_symbol_names` mirrors every `code_symbols` insert through the
materialized view `code_symbol_names_mv`; the view carries
`kind`, `tombstoned_at` and `version`, so `name_candidates` is one statement
with the argMax-by-version liveness form. These tests pin:

- the statement shape (side table only, argMax by version, no FINAL) and
  the app-side fold in place of a DISTINCT;
- the mirror rule: every writer of `code_symbols` goes through
  `CodeRepo.insert_rows` / `build_row` (the view sees every insert), nothing
  writes `code_symbol_names` directly, and the view's SELECT in
  schemas/schema.sql carries exactly the columns the reader aggregates.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from server.code.store.index import CodeRepo
from tests.unit.schema_ddl import column_names, ddl

ROOT = Path(__file__).resolve().parents[2]


class StubCH:
    def __init__(self, returns: list[list[dict[str, Any]] | Exception] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.returns = list(returns or [])

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((sql, params or {}))
        nxt = self.returns.pop(0) if self.returns else []
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _side_rows() -> list[dict[str, Any]]:
    # the same (symbol_id, qualified_name, kind) under both tiers: a plain
    # DISTINCT would fold them to one candidate; `dead` is tombstoned in the main
    # table and never reaches the reader (HAVING is_live = 1 drops it).
    return [
        {
            "name": "foo",
            "tier": "treesitter",
            "qualified_name": "ns::foo",
            "symbol_id": "sA",
            "kind": "function",
            "is_live": 1,
        },
        {
            "name": "foo",
            "tier": "scip",
            "qualified_name": "ns::foo",
            "symbol_id": "sA",
            "kind": "function",
            "is_live": 1,
        },
        {
            "name": "foo",
            "tier": "treesitter",
            "qualified_name": "Bar::foo",
            "symbol_id": "sB",
            "kind": "method",
            "is_live": 1,
        },
    ]


@pytest.mark.asyncio
async def test_name_candidates_reads_one_statement_with_the_liveness_form() -> None:
    ch = StubCH([_side_rows()])
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    out = await repo.name_candidates("o", "n", ["foo", "nothing"])
    assert len(ch.calls) == 1, "the one read"
    (read,) = ch.calls
    assert "FROM loom.code_symbol_names" in read[0]
    assert "code_symbols" not in read[0] and "FINAL" not in read[0]
    assert "argMax(kind, version) AS kind" in read[0]
    assert "argMax(tombstoned_at IS NULL, version) AS is_live" in read[0]
    assert "HAVING is_live = 1" in read[0]
    assert "GROUP BY name, tier, qualified_name, symbol_id" in read[0]
    assert read[1] == {"org": "o", "ns": "n", "names": ["foo", "nothing"]}
    # the two tiers of sA fold to one candidate, as DISTINCT did; sorted
    assert out == {"foo": [("sA", "ns::foo", "function"), ("sB", "Bar::foo", "method")]}


@pytest.mark.asyncio
async def test_no_names_reads_nothing() -> None:
    ch = StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    assert await repo.name_candidates("o", "n", []) == {}
    assert ch.calls == []


# --------------------------------------------------------------------------
# the mirror rule
# --------------------------------------------------------------------------

_READER_COLUMNS = ("kind", "tombstoned_at", "version")


def _view_select_columns(text: str) -> list[str]:
    """Column list of the `code_symbol_names_mv` SELECT."""
    m = re.search(r"SELECT(.*?)FROM\s+loom\.code_symbols", text, flags=re.S)
    assert m, "view SELECT not found"
    return [c.strip().strip("`") for c in m.group(1).replace("\n", " ").split(",")]


def test_schema_dump_carries_the_widened_table_and_view() -> None:
    table = ddl("code_symbol_names")
    for col in _READER_COLUMNS:
        assert col in column_names("code_symbol_names"), col
    # rows written before the backfill sort below it
    assert "`version` UInt64 DEFAULT 0" in table
    assert _view_select_columns(ddl("code_symbol_names_mv")) == [
        "org",
        "namespace",
        "name",
        "tier",
        "qualified_name",
        "symbol_id",
        *_READER_COLUMNS,
    ]


def test_reader_aggregates_only_columns_the_view_mirrors() -> None:
    src = (ROOT / "server/code/store/index/symbols.py").read_text()
    body = src.split("async def name_candidates(", 1)[1].split("async def ", 1)[0]
    used = set(re.findall(r"argMax\((\w+)(?: IS NULL)?, version\)", body))
    assert used == {"kind", "tombstoned_at"}
    assert "GROUP BY name, tier, qualified_name, symbol_id" in body


def test_every_code_symbols_writer_goes_through_the_funnel_the_view_watches() -> None:
    """The view fires on INSERT into loom.code_symbols; a writer that bypassed
    `CodeRepo.insert_rows` with raw SQL, or wrote `code_symbol_names`
    directly, would put the two tables out of step."""
    offenders: list[str] = []
    for rel in ("server", "scripts", "bench"):
        for p in (ROOT / rel).rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"INSERT INTO\s+loom\.code_symbol(s|_names)\b", text):
                offenders.append(str(p.relative_to(ROOT)))
            if re.search(r"insert\(\s*['\"]loom\.code_symbol(s|_names)['\"]", text):
                offenders.append(str(p.relative_to(ROOT)))
    assert offenders == [], offenders
    # every `"code_symbols"` write site is an insert_rows/build_row/tombstone_of call,
    # or scip_delta's `_publish`, which checks the entry lease and calls insert_rows
    for rel in ("server/code/app/index/ingest.py", "server/code/app/index/scip_delta.py"):
        text = (ROOT / rel).read_text()
        for m in re.finditer(r'"code_symbols"', text):
            line = text[text.rfind("\n", 0, m.start()) + 1 : text.find("\n", m.end())]
            assert re.search(
                r"insert_rows\(|_publish\(|build_row\(|tombstone_of\(|moved_of\(|^\s*\"code_symbols\",$",
                line,
            ), (
                rel,
                line.strip(),
            )
