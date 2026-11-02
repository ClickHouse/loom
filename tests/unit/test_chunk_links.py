"""Chunk provenance rows in loom.memory_chunk_sources: the cross product
`write_memory_chunks` writes. Every reader and writer goes through
`server.memory.app.chunk_links`, and erasure tombstones the table. CH-free."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from server.erasure.store import census as erasure_census
from server.erasure.store import steps_graph as graph_steps
from server.memory.app import chunk_links as cl

NOW = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
CHUNKS = [(0, "a" * 64, 1, 10), (1, "b" * 64, 8, 20), (2, "c" * 64, 30, 40)]
KEYS = ["k1", "k2", "k1"]  # a repeated key: the writer's list is not deduplicated
REFS = [(1, 9, "e1"), (9, 25, "e2"), (100, 200, "e9")]  # e9 overlaps no chunk


def _prov(**over: Any) -> list[dict[str, Any]]:
    args: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "agent": "ag",
        "session_id": "s1",
        "chunks": CHUNKS,
        "parent_keys": KEYS,
        "evidence_refs": REFS,
        "now": NOW,
    }
    return cl.provenance_rows(**{**args, **over})


def test_rows_are_the_cross_product_of_chunks_keys_and_overlapping_turns() -> None:
    rows = _prov()
    # chunks 0 and 1 overlap e1 and e2; chunk 2 overlaps nothing -> [None]
    per_chunk = {0: 2, 1: 2, 2: 1}
    assert len(rows) == sum(len(KEYS) * n for n in per_chunk.values()) == 15
    assert {r_["memory_key"] for r_ in rows} == {"k1", "k2"}
    assert [r_["evidence_id"] for r_ in rows if r_["chunk_index"] == 2] == [None, None, None]
    assert all(r_["content_sha256"] and r_["line_end"] >= r_["line_start"] for r_ in rows)


def test_rows_without_parents_or_evidence_keep_the_placeholders() -> None:
    rows = _prov(agent="", session_id="s", chunks=CHUNKS[:1], parent_keys=[], evidence_refs=[])
    assert rows == [
        {
            "org": "o",
            "namespace": "n",
            "agent": "",
            "session_id": "s",
            "chunk_index": 0,
            "content_sha256": "a" * 64,
            "memory_key": "",
            "evidence_id": None,
            "line_start": 1,
            "line_end": 10,
            "created_at": NOW,
            "tombstoned_at": None,
        }
    ]


def test_import_rows_are_one_per_chunk_per_non_empty_key() -> None:
    specs = [
        ("s1", 0, "a" * 64, 1, 10, ["k1", "k2"]),
        ("s1", 1, "b" * 64, 8, 20, ["k2", ""]),
        ("s2", 0, "c" * 64, 0, 0, ["k3"]),
    ]
    rows = cl.import_link_rows(org="o", namespace="n", agent="ag", chunks=specs, now=NOW)
    assert len(rows) == 4
    assert all(r_["evidence_id"] is None for r_ in rows)


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        return self.rows


def test_export_keys_read_per_chunk() -> None:
    ch = _CH([{"session_id": "s1", "chunk_index": 1, "memory_keys": ["k2", "k1"]}])
    links = asyncio.run(
        cl.chunk_memory_keys(ch, org="o", namespace="n", chunks=[("s1", 0), ("s1", 1)])
    )
    assert links == {("s1", 1): ["k1", "k2"]}
    sql, params = ch.calls[0]
    assert f"FROM {cl.SOURCES_TABLE} FINAL" in sql and "GROUP BY session_id, chunk_index" in sql
    assert "PREWHERE" not in sql and params["sessions"] == ["s1"]


def test_export_keys_with_no_chunks_reads_nothing() -> None:
    ch = _CH([])
    assert asyncio.run(cl.chunk_memory_keys(ch, org="o", namespace="n", chunks=[])) == {}
    assert ch.calls == []


def test_retry_builds_what_the_write_path_would_have() -> None:
    """The projection-failure retry and the extraction writer share the
    builder, so for the same chunks, keys and refs they produce the same rows."""
    assert _prov() == _prov(chunks=list(CHUNKS), parent_keys=list(KEYS), evidence_refs=list(REFS))


class _ErasureCH:
    def __init__(self) -> None:
        self.execute_calls: list[str] = []

    async def query_one(self, sql, params=None):
        return {"n": 1}

    async def execute(self, sql, params=None):
        self.execute_calls.append(" ".join(sql.split()))

    async def query_all(self, sql, params=None):
        if "system.mutations" not in sql:  # the user's memory keys
            return [{"namespace": "ns", "memory_key": "k"}]
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]


def test_erasure_tombstones_the_provenance_tables_in_both_scopes() -> None:
    assert graph_steps._LINK_TABLES_TOMBSTONE == (
        "loom.memory_belief_sources",
        "loom.memory_chunk_sources",
    )
    for run in (
        lambda ch: graph_steps.tombstone_source_links_by_namespace("o", "ns", ch=ch),
        lambda ch: graph_steps.tombstone_source_links_by_user("o", "alice", ch=ch),
    ):
        ch = _ErasureCH()
        asyncio.run(run(ch))
        assert [s.split("ALTER TABLE ")[1].split()[0] for s in ch.execute_calls] == list(
            graph_steps._LINK_TABLES_TOMBSTONE
        )
    assert "loom.memory_chunk_sources" in erasure_census.census_tables("user_id")
