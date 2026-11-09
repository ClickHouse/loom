"""The write-time conflict peer read is two statements — stage
A ranks the namespace's vectors alone (the shape the Quantized codec answers
codes-first), stage B reads the candidates' rows by sort key with the row-side
filters — and it answers exactly what the one-statement join answered."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from server.memory.app import conflicts as cf
from server.memory.store import conflict_candidates as conflict_candidates

_BASE = {
    "org": "o",
    "ns": "n",
    "mt": "semantic",
    "self_key": "me",
    "emb": [0.1, 0.2],
    "max_dist": 0.3,
    "dim": 2,
}


class _TwoStageCH:
    """Vectors (stage A) and live rows (stage B) kept apart, as in ClickHouse:
    a vector whose row is superseded / another type is in `vectors` only."""

    def __init__(self, vectors: list[tuple[str, float]], live: set[str]) -> None:
        self.vectors = sorted(vectors, key=lambda v: v[1])
        self.live = live
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        self.calls.append((sql, params))
        if "loom.memory_embeddings FINAL" in sql and "JOIN" not in sql:
            return [
                {"agent": "a", "user_id": "-", "session_id": "-", "memory_key": k, "dist": d}
                for k, d in self.vectors[: int(params["k"])]
            ]
        if "{ids:Array(Tuple" in sql:
            return [
                {
                    "memory_id": f"id-{k}",
                    "memory_key": k,
                    "value": f"v {k}",
                    "pinned": False,
                    "agent": a,
                    "user_id": u,
                    "session_id": s,
                    "run_id": "-",
                    "valid_at": None,
                }
                for (a, u, s, k) in params["ids"]
                if k in self.live
            ]
        raise AssertionError(f"unexpected statement: {sql[:80]}")


def _run(ch: _TwoStageCH) -> list[dict[str, Any]]:
    return asyncio.run(cf._peer_rows(ch, dict(_BASE)))


def test_the_nearest_live_rows_come_back_nearest_first_ties_by_key() -> None:
    ch = _TwoStageCH([("b", 0.10), ("a", 0.10), ("c", 0.05), ("far", 0.9)], {"a", "b", "c", "far"})
    out = _run(ch)
    assert [r["memory_key"] for r in out] == ["c", "a", "b"]
    assert all(r["dist"] <= 0.3 for r in out) and len(ch.calls) == 2


def test_rows_the_row_filters_drop_widen_stage_a_until_ten_survive() -> None:
    """40 vectors of superseded / other-type rows sit nearest; the join would
    skip them and still return the next ten live peers — so must this."""
    dead = [(f"dead{i:02d}", 0.01 + i * 0.001) for i in range(40)]
    alive = [(f"live{i:02d}", 0.2 + i * 0.001) for i in range(12)]
    ch = _TwoStageCH(dead + alive, {k for k, _ in alive})
    out = _run(ch)
    assert [r["memory_key"] for r in out] == [f"live{i:02d}" for i in range(10)]
    ks = [p["k"] for sql, p in ch.calls if "k" in p]
    assert ks == [32, 128], "one widening round, x4"


def test_a_candidate_past_max_dist_ends_the_search() -> None:
    ch = _TwoStageCH([("x", 0.1), ("y", 0.5)] + [(f"z{i}", 0.6) for i in range(40)], {"x"})
    assert [r["memory_key"] for r in _run(ch)] == ["x"]
    assert sum(1 for _, p in ch.calls if "k" in p) == 1, "no widening once past the ceiling"


def test_the_widening_cap_falls_back_to_the_join(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _TwoStageCH([(f"d{i:04d}", 0.01) for i in range(3000)], set())
    joined: list[dict[str, Any]] = []

    async def _joined(
        ch_: Any, params: dict[str, Any], in_scope: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        joined.append(params)
        return [{"memory_key": "from-join", "dist": 0.02}]

    monkeypatch.setattr(cf, "_peer_rows_joined", _joined)
    assert _run(ch) == [{"memory_key": "from-join", "dist": 0.02}] and joined


def test_the_gates_own_stage_a_reads_every_type() -> None:
    assert "memory_type" not in conflict_candidates._peer_candidates_sql()


def test_the_pinned_arm_takes_type_from_the_memory_row_never_the_vector() -> None:
    sql = conflict_candidates.PINNED_PEERS_SQL
    assert "m.pinned = 1" in sql and "m.memory_type" in sql
    # Every type read is the memory row's (`m.`); a bare one would be the vector row's.
    assert not re.search(r"(?<![.\w])memory_type", sql)
    assert "LIMIT 10" in sql


def test_stage_a_keeps_the_codes_first_shape_and_never_decodes_the_column() -> None:
    sql = conflict_candidates._peer_candidates_sql()
    assert "ORDER BY dist ASC" in sql and "LIMIT {k:UInt32}" in sql
    assert "embedding.size0 = {dim:UInt16}" in sql
    # length()/notEmpty() read the full-precision column: codes-first off.
    assert "length(embedding)" not in sql and "notEmpty(embedding)" not in sql
    assert "value" not in sql and "JOIN" not in sql
    assert "memory_key != {self_key:String}" in sql
    rows = conflict_candidates._PEER_ROWS_SQL
    assert "PREWHERE" in rows and "memory_type = {mt:String}" in rows
    assert "status IN ('active', 'conflicted')" in rows and "tombstoned_at IS NULL" in rows


def test_the_joined_fallback_takes_the_callers_limit() -> None:
    seen: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            seen.append(sql)
            return []

    asyncio.run(cf._peer_rows_joined(_CH(), dict(_BASE), limit=7))
    asyncio.run(cf._peer_rows_joined(_CH(), dict(_BASE)))
    assert "LIMIT 7" in seen[0] and f"LIMIT {cf.PEER_LIMIT}" in seen[1]
