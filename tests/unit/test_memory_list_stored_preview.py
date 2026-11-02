"""A list never reads the value column.

A 140-character card preview cut from `value` would still read the whole
column (even as a substring), megabytes per page on a namespace of large
values. The stored `value_preview` / `value_len`
MATERIALIZED columns carry what a list needs; the whole value stays for
`memory.get`. The `total` / `today` counts under FINAL are remembered per
namespace version."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from tests.unit.schema_ddl import ddl


class _CaptureCH:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(" ".join(sql.split()))
        return []


@pytest.mark.asyncio
async def test_a_preview_read_selects_the_stored_columns_and_never_the_value() -> None:
    ch = _CaptureCH()
    ident = memory_repo.Identity(
        org="acme", namespace="team", agent="*", user_id="*", session_id="*"
    )
    await memory_repo.list_by_namespace(
        ident, limit=10, offset=0, preview=True, ch=ch, scope=WHOLE_NAMESPACE
    )
    # The preview page locates first (no preview, no
    # value) and reads the previews by pairs in a second statement; the fake
    # answers no rows, so only the locate statement runs here.
    sql = ch.sql[-1]
    assert "value_len > 2048 AS value_truncated" in sql and "value_preview" not in sql
    select = sql[sql.index("SELECT") : sql.index("FROM loom.memory")]
    tokens = [t.strip() for t in select.replace("SELECT", "").split(",")]
    assert "value" not in tokens, "the value column must not be selected on the preview path"


@pytest.mark.asyncio
async def test_a_whole_value_read_still_selects_the_value() -> None:
    ch = _CaptureCH()
    ident = memory_repo.Identity(
        org="acme", namespace="team", agent="*", user_id="*", session_id="*"
    )
    await memory_repo.list_by_namespace(ident, limit=10, offset=0, ch=ch, scope=WHOLE_NAMESPACE)
    select = ch.sql[-1][ch.sql[-1].index("SELECT") : ch.sql[-1].index("FROM loom.memory")]
    tokens = [t.strip() for t in select.replace("SELECT", "").split(",")]
    assert "value" in tokens and "value_preview AS value" not in ch.sql[-1]


def test_the_score_path_reads_the_stored_preview() -> None:
    from server.memory.app import query as _memory_query

    src = inspect.getsource(_memory_query._list_by_score)
    assert "value_preview AS value" in src and "value_len > 2048 AS value_truncated" in src
    assert "substring(value" not in src


def test_a_truncated_preview_row_reads_its_stored_hash_not_the_preview() -> None:
    from server.core.hashing import sha256_value_json
    from server.memory.domain.trust import row_content_sha256 as _row_content_sha256

    row = memory_repo.MemoryRow(
        memory_id="00000000-0000-0000-0000-000000000001",
        org="acme",
        namespace="team",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="x" * 2048,
        value_truncated=True,
        confidence=1.0,
        version=1,
        pinned=False,
        expires_at=None,
        created_at="2026-09-19 00:00:00",
        updated_at="2026-09-19 00:00:00",
        content_sha256="ab" * 32,
    )
    assert _row_content_sha256(row) == "ab" * 32
    row2 = row.model_copy(update={"content_sha256": ""})
    assert _row_content_sha256(row2) == "", (
        "a cut preview without a stored hash cannot be confirmed"
    )
    row3 = row.model_copy(update={"content_sha256": "", "value_truncated": False, "value": "whole"})
    assert _row_content_sha256(row3) == sha256_value_json('"whole"')


@pytest.mark.asyncio
async def test_counts_are_remembered_per_namespace_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list, reset_list_counts_cache

    reset_list_counts_cache()
    calls = {"counts": 0, "version": 4200}

    async def nsv(org: str, ns: str, **kw: Any) -> int:
        return calls["version"]

    async def lst(identity: Any, **kw: Any) -> list[Any]:
        return []

    async def cnt(identity: Any, **kw: Any) -> int:
        calls["counts"] += 1
        return 7

    monkeypatch.setattr(memory_repo, "namespace_version", nsv)
    monkeypatch.setattr(memory_repo, "list_by_namespace", lst)
    monkeypatch.setattr(memory_repo, "count_by_namespace", cnt)
    inp = MemoryListInput(org="acme", namespace="team", agent="*", limit=10)
    out = await _handle_memory_list(inp)
    assert (out.total, out.today) == (7, 7) and calls["counts"] == 2
    await _handle_memory_list(inp)
    assert calls["counts"] == 2, "same version, same day: no second pair of FINAL counts"
    await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", limit=10, prefix="p/")
    )
    assert calls["counts"] == 4, "a different scope is its own count"
    calls["version"] = 4201
    await _handle_memory_list(inp)
    assert calls["counts"] == 6, "a moved version recomputes"
    reset_list_counts_cache()


def test_both_tables_carry_the_stored_preview_columns() -> None:
    for table in ("memory", "memory_versions"):
        block = ddl(table)
        assert "`value_preview` String MATERIALIZED substring(value, 1, 2048)" in block
        assert "`value_len` UInt32 MATERIALIZED length(value)" in block
    assert "value_preview" not in memory_repo.MEMORY_PERSISTED_COLS, (
        "a MATERIALIZED column is never inserted"
    )
