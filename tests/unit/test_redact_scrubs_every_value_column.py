"""memory.redact scrubs every column that holds a version's text.

The version row carries its text in `value`, `value_json` (which the FINAL
projection renders when it is populated), `evidence`, `entities` and
`concepts`, on loom.memory and on the memory_versions mirror. The key's
embedding and chunk rows carry the text once more, per key. The columns are
listed in `memory_repo.redaction`. These tests read the statements a
redaction issues from a stub client, apply their assignments to real rows,
and check that no copy of the text survives, including the row that
namespace.rollback would reinstate. CH-free.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import pytest

from server.memory import store as mr
from server.memory.store import redaction
from server.memory.store.rows import MEMORY_PERSISTED_COLS
from tests.unit.clickhouse_local import CLICKHOUSE, run
from tests.unit.memory_rows import projection_row
from tests.unit.schema_ddl import column_names

SECRET = "ssn 078-05-1120"
_MID = "33333333-3333-3333-3333-333333333333"


class _StubCH:
    def __init__(self, live: dict[str, list[Any]] | None = None) -> None:
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.live = live or {}

    async def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, dict(params or {})))

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        return {"n": 1}

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "AS deleted" in sql:
            return []  # the key's history: no version after the redacted one
        table = "embeddings" if "memory_embeddings" in sql else "chunks"
        return [{"t": t} for t in self.live.get(table, [])]


def _table(sql: str) -> str:
    m = re.search(r"ALTER TABLE (\S+)", sql)
    assert m, sql
    return m.group(1)


def _assignments(sql: str) -> dict[str, str]:
    flat = " ".join(sql.split())
    body = flat.split(" UPDATE ", 1)[1].rsplit(" WHERE ", 1)[0]
    parts = re.split(r",\s*(?=\w+\s*=)", body)
    return {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip() for p in parts}


def _literal(expr: str) -> Any:
    if expr == "[]":
        return []
    if expr.startswith("'"):
        return expr[1:-1]
    if expr.isdigit():
        return int(expr)
    return expr  # a bound parameter; not text


async def _redact() -> list[tuple[str, dict[str, Any]]]:
    ch = _StubCH()
    await mr.redact_versions(
        ("acme", "n", "a", "u1", "-", "customer"),
        _MID,
        17,
        when=datetime(2026, 1, 1),
        reason_blob="pii|admin",
        head=None,
        ch=ch,
    )
    return ch.statements


def _stored_text_columns(table: str) -> set[str]:
    return {
        c
        for c in column_names(table, materialized=False)
        if c in {"value", "value_json", "evidence", "entities", "concepts"}
    }


async def test_both_version_tables_scrub_every_text_column() -> None:
    statements = await _redact()
    assert [_table(s) for s, _ in statements] == ["loom.memory", "loom.memory_versions"]
    for sql, params in statements:
        cols = set(_assignments(sql))
        name = _table(sql).removeprefix("loom.")
        assert _stored_text_columns(name) <= cols, sorted(_stored_text_columns(name) - cols)
        assert {"redacted_at", "redaction_reason"} <= cols
        # Only the named version and its later copies on the key (none here): the other
        # versions share the memory_id.
        assert "memory_id = {mid:UUID}" in sql and "(version = {v:UInt64}" in sql
        assert (params["v"], params["copies"]) == (17, [])


async def test_a_rollback_to_a_redacted_version_never_revives_the_object() -> None:
    """namespace.rollback reinstates a mirror row through fetch_version_rows
    (hydrate) and build_memory_version_row: the new head must carry no text."""
    stored = projection_row(
        org="acme",
        namespace="shop",
        memory_key="customer",
        memory_id=_MID,
        version=17,
        value=json.dumps({"note": SECRET}),
        value_json=json.dumps({"note": SECRET}),
        evidence=f"said: {SECRET}",
        entities=["078-05-1120"],
        concepts=["ssn"],
    )
    sql = next(s for s, _ in await _redact() if "memory_versions" in s)
    for col, expr in _assignments(sql).items():
        if col in stored and not expr.startswith("{"):
            stored[col] = _literal(expr)
    # The mirror row as SELECT * returns it: `value` is the stored text.
    mirror_row = {k: v for k, v in stored.items() if k != "value_text"}
    reinstated = mr.build_memory_version_row(mr.rows._hydrate(mirror_row), version=99)
    assert set(reinstated) >= set(MEMORY_PERSISTED_COLS)
    assert reinstated["value_json"] in ("{}", "")
    assert reinstated["value"] == redaction.REDACTED_VALUE
    assert SECRET not in repr(reinstated)


@pytest.mark.parametrize("head_live", [True, False])
async def test_derived_rows_are_purged_except_the_live_heads(head_live: bool) -> None:
    live = {"embeddings": ["the current value"], "chunks": [b"a" * 64]}
    ch = _StubCH(live)
    scrubs = await mr.key_derivative_scrubs(
        "acme",
        "shop",
        "a",
        "-",
        "-",
        "customer",
        head_live=head_live,
        through=2**63,
        ch=ch,
        set_chunks_kept=None,
    )
    counts = await mr.run_scrubs(scrubs, apply=True, ch=ch)
    assert counts == {
        "loom.memory_embeddings": 1,
        "loom.memory_chunks": 1,
        "loom.memory_code_links": 1,
    }
    emb_sql, emb_params = ch.statements[0]
    chunk_sql, chunk_params = ch.statements[1]
    links_sql, _ = ch.statements[2]
    # the suggestions deleted links kept; the live head's links are its own
    assert links_sql.startswith("ALTER TABLE loom.memory_code_links UPDATE suggestion = ''")
    # every row not made from the live head's value: superseded, deleted, not relinked yet
    assert "inputs_hash != {live:String}" in links_sql and "memory_key = {k:String}" in links_sql
    assert "tombstoned_at" not in links_sql
    # Deleted, never UPDATEd: an update of the vector table leaves patch parts.
    assert emb_sql.startswith("DELETE FROM loom.memory_embeddings")
    assert chunk_sql.startswith("DELETE FROM loom.memory_chunks")
    assert "memory_key = {k:String}" in emb_sql and "NOT IN {live:Array(String)}" in emb_sql
    assert "parent_memory_key = {k:String}" in chunk_sql
    assert "NOT IN {live:Array(FixedString(64))}" in chunk_sql
    if head_live:
        assert emb_params["live"] == ["the current value"]
        assert chunk_params["live"] == ["a" * 64]
    else:
        assert emb_params["live"] == [] and chunk_params["live"] == []


async def test_nothing_is_rewritten_where_no_row_holds_text() -> None:
    class _Empty(_StubCH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
            return {"n": 0}

    ch = _Empty()
    scrubs = await mr.key_derivative_scrubs(
        "acme",
        "shop",
        "a",
        "-",
        "-",
        "customer",
        head_live=False,
        through=2**63,
        ch=ch,
        set_chunks_kept=None,
    )
    assert await mr.run_scrubs(scrubs, apply=True, ch=ch) == {
        "loom.memory_embeddings": 0,
        "loom.memory_chunks": 0,
        "loom.memory_code_links": 0,
    }
    assert ch.statements == []


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_search_row_purge_judges_by_real_hash_version_bound_and_fence() -> None:
    """A row without a real hash (none, the placeholder '0', NUL-padded or not, or one not
    in lowercase hex) is judged by its excerpt, also past the redaction's version bound,
    so the live head's text keeps its row. A real hash is judged by itself: a row stamped
    after the bound is a newer write's, unless it carries the redacted text. A fence
    stays."""
    live_sha, old_sha, redacted_sha = "a" * 64, "b" * 64, "c" * 64
    ch = _StubCH({"embeddings": ["the current value"]})
    scrubs = await mr.key_derivative_scrubs(
        "acme",
        "shop",
        "a",
        "-",
        "-",
        "customer",
        head_live=True,
        through=10,
        live_sha=live_sha,
        redacted_sha=redacted_sha,
        ch=ch,
        set_chunks_kept=None,
    )
    await mr.run_scrubs(scrubs, apply=True, ch=ch)
    emb_sql, emb_params = ch.statements[0]
    rows = [
        ("0", "the current value", 1, 1),
        ("0", "the current value", 20, 1),
        ("0", "an older value", 1, 1),
        ("0", "an older value", 20, 1),
        ("", "a pre-hash value", 20, 1),
        ("0" + "\\0" * 63, "a padded placeholder", 20, 1),
        ("A" * 64, "an uppercase digest", 20, 1),
        (live_sha, "the current value", 1, 1),
        (old_sha, "an older value", 1, 1),
        (old_sha, "a newer write", 20, 1),
        (redacted_sha, "the redacted value", 20, 1),
        ("", "", 1, 0),  # a fence: no text
    ]
    values = ", ".join(f"('{h}', '{t}', {v}, {d})" for h, t, v, d in rows)
    # The columns the purge reads, typed as loom.memory_embeddings has them (its own DDL
    # needs a full-width vector on every row).
    setup = [
        "CREATE TABLE loom.memory_embeddings (org String, namespace String, agent String, "
        "user_id String, session_id String, memory_key String, content_sha256 String, "
        "content_excerpt String, version UInt64, embedding_dim UInt16, "
        "entities Array(LowCardinality(String)), concepts Array(String)) ENGINE = Memory",
        "INSERT INTO loom.memory_embeddings SELECT 'acme', 'shop', 'a', '-', '-', 'customer', "
        f"h, t, v, d, [], [] FROM values('h String, t String, v UInt64, d UInt16', {values})",
    ]
    where = emb_sql.split("\nWHERE ", 1)[1].split("\nSETTINGS", 1)[0]
    purged = run(
        setup,
        "SELECT content_sha256 AS h, content_excerpt AS t, version AS v "
        f"FROM loom.memory_embeddings WHERE {where}",
        emb_params,
    )
    assert sorted((r["t"], int(r["v"])) for r in purged) == [
        ("a padded placeholder", 20),
        ("a pre-hash value", 20),
        ("an older value", 1),
        ("an older value", 1),
        ("an older value", 20),
        ("an uppercase digest", 20),
        ("the redacted value", 20),
    ]


def test_every_scrub_writes_the_one_placeholder() -> None:
    """The scrubs spell the placeholder per statement (the mutation inventory reads each
    rewrite whole); they all write the module's constant."""
    from server.memory.store import pg_memory

    for sql in (redaction._MEMORY_SCRUB, redaction._VERSIONS_SCRUB, pg_memory._SCRUBBED):
        assert f"'{redaction.REDACTED_VALUE}'" in sql


def test_the_authoritys_scrub_clears_the_columns_clickhouse_clears() -> None:
    """Under Postgres a redaction rewrites the heads and the journal (`pg_memory._SCRUBBED`):
    it writes the cleared value the ClickHouse scrub writes to each of its columns, and
    records when and why."""
    from server.memory.store import pg_memory

    cleared = {
        col: "'[]'::jsonb" if v == "[]" else v
        for col, v in _assignments(redaction._MEMORY_SCRUB).items()
    }
    assert pg_memory._SCRUB_VALUES == cleared
    # Last, so no cleared value overwrites them.
    assert pg_memory._SCRUBBED.endswith("'redacted_at', $10::text, 'redaction_reason', $11::text)")
