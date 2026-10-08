"""Memory.related ranks on the columns that rank.

A candidate scan that read `substring(value, 1, 1200)` for every row sharing
an entity with the source would read tens of MB to return four hits. It reads
the key columns and `entities` only, keeps
the top_k, and fetches the winners' excerpts from the stored 2 KB preview
by their exact `(memory_id, version)` without FINAL.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.store import Identity

_IDENT = Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")


def _select_list(sql: str) -> str:
    m = re.search(r"SELECT\s+(.*?)\s+FROM", sql, re.S)
    assert m, sql
    return m.group(1)


class _CH:
    def __init__(self) -> None:
        self.sqls: list[str] = []
        self.params: list[dict[str, Any] | None] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sqls.append(sql)
        self.params.append(params)
        return {"entities": ["lane-1", "exporter"]}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        self.params.append(params)
        if "value_preview" in sql:
            return [
                {"memory_id": "m2", "version": 5, "content_excerpt": "second"},
                {"memory_id": "m1", "version": 9, "content_excerpt": "first"},
            ]
        return [
            {
                "memory_id": "m1",
                "memory_key": "k1",
                "memory_type": "semantic",
                "version": 9,
                "shared_entities": ["lane-1", "exporter"],
                "overlap_count": 2,
            },
            {
                "memory_id": "m2",
                "memory_key": "k2",
                "memory_type": "episodic",
                "version": 5,
                "shared_entities": ["lane-1"],
                "overlap_count": 1,
            },
        ]


@pytest.mark.asyncio
async def test_related_ranks_without_the_value_then_fetches_the_winners_previews() -> None:
    ch = _CH()
    ents, rows = await memory_repo.find_related_by_entities(
        _IDENT, "src", top_k=5, min_overlap=1, ch=ch
    )
    assert ents == ["lane-1", "exporter"]
    assert [(r["memory_key"], r["content_excerpt"], r["overlap_count"]) for r in rows] == [
        ("k1", "first", 2),
        ("k2", "second", 1),
    ]
    source, rank, fetch = ch.sqls
    assert "SELECT entities" in source
    assert "FROM loom.memory FINAL" in rank and "hasAny(entities" in rank
    assert not re.search(r"\bvalue\b", _select_list(rank)), "the ranking scan reads no value column"
    assert "version" in _select_list(rank) and "memory_id" in _select_list(rank)
    assert "FINAL" not in fetch and "value_preview" in fetch
    assert "(memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}" in fetch
    assert ch.params[2] is not None and ch.params[2]["pairs"] == [("m1", 9), ("m2", 5)]


@pytest.mark.asyncio
async def test_related_with_no_candidates_issues_no_fetch() -> None:
    class _Empty(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            self.sqls.append(sql)
            return []

    ch = _Empty()
    ents, rows = await memory_repo.find_related_by_entities(
        _IDENT, "src", top_k=5, min_overlap=1, ch=ch
    )
    assert ents == ["lane-1", "exporter"] and rows == []
    assert len(ch.sqls) == 2  # source entities + the ranking scan


@pytest.mark.asyncio
async def test_a_client_answering_excerpts_in_the_scan_short_circuits() -> None:
    """Older fakes answer the scan with excerpts already in place."""

    class _Old(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            self.sqls.append(sql)
            return [
                {
                    "memory_key": "k1",
                    "memory_type": "semantic",
                    "content_excerpt": "x",
                    "shared_entities": ["lane-1"],
                    "overlap_count": 1,
                }
            ]

    ch = _Old()
    _, rows = await memory_repo.find_related_by_entities(
        _IDENT, "src", top_k=5, min_overlap=1, ch=ch
    )
    assert rows[0]["content_excerpt"] == "x" and len(ch.sqls) == 2


@pytest.mark.asyncio
async def test_two_identities_sharing_a_key_and_version_each_get_their_own_excerpt() -> None:
    """The entity pass stamps one version on every row of its batch, the same
    key under two agents included; the excerpts are keyed on memory_id."""

    class _Twin(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            self.sqls.append(sql)
            self.params.append(params)
            if "value_preview" in sql:
                return [
                    {"memory_id": "mb", "version": 7, "content_excerpt": "agent b"},
                    {"memory_id": "ma", "version": 7, "content_excerpt": "agent a"},
                ]
            return [
                {
                    "memory_id": mid,
                    "memory_key": "k",
                    "memory_type": "semantic",
                    "version": 7,
                    "shared_entities": ["lane-1"],
                    "overlap_count": 1,
                }
                for mid in ("ma", "mb")
            ]

    ch = _Twin()
    _, rows = await memory_repo.find_related_by_entities(
        _IDENT, "src", top_k=5, min_overlap=1, ch=ch
    )
    assert ch.params[2] is not None and ch.params[2]["pairs"] == [("ma", 7), ("mb", 7)]
    assert [r["content_excerpt"] for r in rows] == ["agent a", "agent b"]
