"""memory.delete and summaries built from the deleted row: a dependent whose
sources are all gone goes too; one
with surviving sources is downgraded to `conflicted` with a stale-source tag,
on both tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.store import lifecycle


class _CH:
    def __init__(self) -> None:
        self.all_answers: list[list[dict[str, Any]]] = []
        self.one_answers: list[dict[str, Any] | None] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append(("all", " ".join(sql.split()), params or {}))
        return self.all_answers.pop(0) if self.all_answers else []

    async def query_one(self, sql, params=None):
        self.calls.append(("one", " ".join(sql.split()), params or {}))
        return self.one_answers.pop(0) if self.one_answers else None

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows))


def _dep(mid: str, key: str, srcs: list[str]) -> dict[str, Any]:
    return {
        "mid": mid,
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "tags": ["consolidated", *[f"src:{s}" for s in srcs]],
    }


@pytest.mark.asyncio
async def test_dependents_query_matches_the_src_tag_of_the_deleted_key() -> None:
    ch = _CH()
    ch.all_answers.append([_dep("m1", "consolidated:t", ["k1", "k2"])])
    deps = await memory_repo.consolidated_dependents_of("o", "ns", "k1", ch=ch)
    assert [d["mid"] for d in deps] == ["m1"]
    _, sql, params = ch.calls[0]
    assert "has(tags, 'consolidated') AND has(tags, {srctag:String})" in sql
    assert params["srctag"] == "src:k1" and "tombstoned_at IS NULL" in sql


@pytest.mark.asyncio
async def test_delete_tombstones_orphaned_summaries_and_downgrades_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()
    # two dependents: one whose only source is k1, one that also cites k2
    ch.all_answers.append(
        [
            _dep("m1", "consolidated:only_k1", ["k1"]),
            _dep("m2", "consolidated:k1_and_k2", ["k1", "k2"]),
        ]
    )
    ch.one_answers.extend([{"n": 0}, {"n": 1}])  # live sources: none / one

    written: list[tuple[list[str], dict[str, Any]]] = []

    async def _write_versions(org, ids, update, *, ch=None):
        row = memory_repo.MemoryRow(
            memory_id=ids[0],
            org=org,
            namespace="ns",
            agent="a",
            user_id="-",
            session_id="-",
            memory_key="x",
            memory_type="semantic",
            value="v",
            confidence=1.0,
            version=1,
            pinned=False,
            created_at=datetime(2026, 1, 1),
            expires_at=None,
            tags=["consolidated", "src:k1", "src:k2"],
        )
        written.append((list(ids), update(row)))
        return len(ids)

    reemits: list[tuple[list[str], dict[str, Any]]] = []

    async def _reemit(org, victims, delta, *, ch):
        reemits.append(([v["mid"] for v in victims], delta))

    monkeypatch.setattr(lifecycle, "write_versions_by_ids", _write_versions)
    monkeypatch.setattr(lifecycle, "_reemit_embedding_rows", _reemit)

    tomb, down = await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch)
    assert (tomb, down) == (1, 1)
    (ids_t, delta_t), (ids_d, delta_d) = written
    assert ids_t == ["m1"] and delta_t["tombstoned_at"] is not None
    assert ids_d == ["m2"] and delta_d["status"] == "conflicted"
    assert delta_d["tags"][-1] == "stale-source:k1" and "src:k2" in delta_d["tags"]
    # both tables: the embeddings table (what recall reads) is re-emitted too
    assert reemits[0][0] == ["m1"] and "tombstoned_at" in reemits[0][1]
    assert reemits[1][0] == ["m2"] and reemits[1][1]["status"] == "conflicted"


@pytest.mark.asyncio
async def test_no_dependents_is_a_noop() -> None:
    ch = _CH()
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 0)
    assert len(ch.calls) == 1
