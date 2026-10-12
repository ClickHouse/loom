"""Every state-of-row change on loom.memory_embeddings is a
versioned re-emit of the live rows — the RMT rule, never a lightweight
UPDATE — and no statement under server/ UPDATEs the vector table."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory import store as M

ROOT = Path(__file__).resolve().parents[2]


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return list(self.rows)

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


_LIVE = {
    "org": "o",
    "namespace": "n",
    "agent": "a",
    "user_id": "u",
    "session_id": "s",
    "memory_key": "k",
    "scope": "private",
    "embedding_model": "m",
    "embedding_dim": 3,
    "embedding": [0.1, 0.2, 0.3],
    "memory_key_embedding": [0.4, 0.5, 0.6],
    "memory_key_embedding_dim": 3,
    "content_excerpt": "text",
    "tags": ["t"],
    "confidence": 0.9,
    "expires_at": datetime(2026, 1, 1),
    "version": 11,
    "reinforcement_count": 4,
    "status": "active",
    "content_excerpt_lc": "text",  # MATERIALIZED: dropped
}


@pytest.mark.asyncio
async def test_reemit_carries_every_persisted_column_and_applies_the_overrides() -> None:
    ch = _CH([dict(_LIVE), {**_LIVE, "embedding_model": "m2"}])
    n = await M.reemit_embedding_rows_for_key(
        "o", "n", "a", "u", "s", "k", ch=ch, expires_at=None, confidence=0.45
    )
    assert n == 2
    sql, params = ch.query_all_calls[0]
    assert "FROM loom.memory_embeddings FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "ns": "n", "a": "a", "u": "u", "s": "s", "k": "k"}
    ((table, rows, settings),) = ch.insert_calls
    assert table == "loom.memory_embeddings"
    assert settings == {"async_insert": 0, "wait_for_async_insert": 1}
    for row in rows:
        assert set(row) >= set(M.EMBEDDING_PERSISTED_COLS)
        assert "content_excerpt_lc" not in row  # MATERIALIZED columns are the server's
        assert row["embedding"] == [0.1, 0.2, 0.3] and row["memory_key_embedding"] == [
            0.4,
            0.5,
            0.6,
        ]
        assert row["expires_at"] is None and row["confidence"] == 0.45
        assert row["reinforcement_count"] == 4 and row["tags"] == ["t"]
        assert row["version"] != 11 and row["version"] > 0
    assert {r["embedding_model"] for r in rows} == {"m", "m2"}


@pytest.mark.asyncio
async def test_reemit_with_no_live_row_inserts_nothing() -> None:
    ch = _CH([])
    assert await M.reemit_embedding_rows_for_key("o", "n", "a", "u", "s", "k", ch=ch, tags=[]) == 0
    assert ch.insert_calls == []


@pytest.mark.asyncio
async def test_tombstone_is_a_reemit_with_tombstoned_at() -> None:
    ch = _CH([dict(_LIVE)])
    ts = datetime(2026, 9, 14, 23, 0, 0)
    assert await M.tombstone_embeddings_for_key("o", "n", "a", "u", "s", "k", ts=ts, ch=ch) == 1
    ((_, rows, _),) = ch.insert_calls
    assert rows[0]["tombstoned_at"] == ts and rows[0]["embedding"] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_shared_reemit_reads_the_scope_shared_rows_of_the_key() -> None:
    ch = _CH([{**_LIVE, "agent": "_shared", "scope": "shared"}])
    ts = datetime(2026, 9, 14)
    assert await M.reemit_shared_embedding_rows("o", "n", "k", ch=ch, tombstoned_at=ts) == 1
    sql, params = ch.query_all_calls[0]
    assert "scope = 'shared'" in sql and "FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "ns": "n", "k": "k"}
    ((_, rows, _),) = ch.insert_calls
    assert rows[0]["scope"] == "shared" and rows[0]["tombstoned_at"] == ts


class _VectorsCH(_CH):
    """Answers the memory_embeddings read with `rows` and the entity_vectors read
    with `entity_rows`."""

    def __init__(self, rows: list[dict[str, Any]], entity_rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.entity_rows = entity_rows

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return list(self.entity_rows if "loom.entity_vectors" in sql else self.rows)


@pytest.mark.asyncio
async def test_retiring_a_key_tombstones_at_the_new_rows_version_and_its_entity_vectors() -> None:
    """The tombstone copy takes the unembedded row's version, not a fresh one: a
    concurrent embedded write's newer vector outranks it. The key's entity vectors
    go too, and only this identity's (the table has no session column)."""
    ent = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "memory_key": "k"}
    other = {**ent, "agent": "someone-else"}
    ch = _VectorsCH([dict(_LIVE)], [dict(ent), dict(other)])
    previous = SimpleNamespace(
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="k",
        memory_type="semantic",
        sensitivity="",
    )
    assert await M.tombstone_vectors_of(previous, version=42, ch=ch) == 1
    (emb_table, emb_rows, _), (ent_table, ent_rows, _) = ch.insert_calls
    assert emb_table == "loom.memory_embeddings"
    assert emb_rows[0]["version"] == 42 and emb_rows[0]["tombstoned_at"] is not None
    assert ent_table == "loom.entity_vectors"
    assert [r["agent"] for r in ent_rows] == ["a"] and ent_rows[0]["tombstoned_at"] is not None


@pytest.mark.asyncio
async def test_a_never_embedded_previous_version_reads_nothing() -> None:
    ch = _VectorsCH([dict(_LIVE)], [])
    previous = SimpleNamespace(memory_type="semantic", sensitivity="high")
    assert await M.tombstone_vectors_of(previous, version=42, ch=ch) == 0
    assert ch.query_all_calls == [] and ch.insert_calls == []


_UPDATE_RE = re.compile(
    r"(?:^|\W)(?:UPDATE\s+(?:loom\.)?memory_embeddings\b|"
    r"ALTER\s+TABLE\s+(?:loom\.)?memory_embeddings\s+UPDATE\b)",
    re.IGNORECASE,
)


def test_nothing_under_server_updates_the_vector_table() -> None:
    """One lightweight UPDATE on
    loom.memory_embeddings leaves a patch part that switches the fact leg's
    codes-first read off for every search."""
    offenders = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _UPDATE_RE.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders
