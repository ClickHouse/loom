"""The write path's peer lookups read the namespace once per
batch of facts, not once per fact."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import conflicts, value_updates
from server.memory.store import conflict_candidates as conflict_candidates


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.query_one_calls.append((sql, params or {}))
        return None


def test_batched_statement_has_one_distance_term_per_fact_and_no_lambda_over_the_vector() -> None:
    sql = conflict_candidates.candidate_sql_many(3)
    for i in (1, 2, 3):
        assert f"({i}, cosineDistance(e.embedding, {{emb{i}:Array(Float32)}}))" in sql
    assert "arrayMap(" not in sql  # would replicate the 6 KB vector per fact per row
    assert "ARRAY JOIN arrayFilter(" in sql and "LIMIT 10 BY qidx" in sql
    assert "{types:Array(String)}[x.1] = m.memory_type" in sql
    assert "{keys:Array(String)}[x.1] != m.memory_key" in sql
    assert "x.2 <= {max_dists:Array(Float32)}[x.1]" in sql
    # The per-fact statement's row predicates are all still there.
    for pred in (
        "m.tombstoned_at IS NULL",
        "m.status IN ('active', 'conflicted')",
        "e.tombstoned_at IS NULL",
        "e.embedding_dim = {dim:UInt16}",
        "length(e.embedding) = {dim:UInt16}",
    ):
        assert pred in sql
    with pytest.raises(ValueError):
        conflict_candidates.candidate_sql_many(0)
    vsql = value_updates._peers_sql_many(2)
    assert vsql.count("1 - cosineDistance(e.embedding, {emb") == 2
    assert "x.2 >= {min_cos:Float32}" in vsql and "LIMIT 8 BY qidx" in vsql


@pytest.mark.asyncio
async def test_candidate_rows_many_chunks_by_batch_and_maps_rows_back_by_qidx() -> None:
    ch = StubCH()
    # 5 facts, batch 2, one dimension → 3 statements (2, 2, 1); a fact with no
    # embedding gets no statement and no entry.
    items = [("semantic", f"k{i}", [0.1 * (i + 1), 0.2], 0.3) for i in range(5)]
    items.append(("semantic", "k5", [], 0.3))
    ch.query_all_returns = [
        [{"qidx": 1, "memory_id": "p", "memory_key": "peer_of_k1", "dist": 0.1}],
        [],
        [{"qidx": 0, "memory_id": "q", "memory_key": "peer_of_k4", "dist": 0.2}],
    ]
    out = await conflicts.candidate_rows_many(org="o", namespace="ns", items=items, batch=2, ch=ch)
    assert len(ch.query_all_calls) == 3
    sizes = [len([k for k in p if k.startswith("emb")]) for _, p in ch.query_all_calls]
    assert sorted(sizes) == [1, 2, 2]
    assert out[1] == [{"qidx": 1, "memory_id": "p", "memory_key": "peer_of_k1", "dist": 0.1}]
    assert out[4] == [{"qidx": 0, "memory_id": "q", "memory_key": "peer_of_k4", "dist": 0.2}]
    assert out[0] == [] and out[2] == [] and out[3] == []
    assert 5 not in out  # no embedding → no entry → caller keeps its per-fact path
    _, p0 = ch.query_all_calls[0]
    assert p0["types"] == ["semantic", "semantic"] and p0["keys"] == ["k0", "k1"]
    assert p0["max_dists"] == [0.3, 0.3] and p0["dim"] == 2


@pytest.mark.asyncio
async def test_check_on_commit_with_prefetched_rows_issues_no_peer_statement(monkeypatch) -> None:
    ch = StubCH()
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)
    rows = [
        {
            "memory_id": "p1",
            "memory_key": "user_city",
            "value": "Lisbon",
            "pinned": False,
            "agent": "a",
            "user_id": "u",
            "session_id": "s",
            "run_id": "-",
            "valid_at": None,
            "dist": 0.05,
        }
    ]
    res = await conflicts.check_on_commit(
        org="o",
        namespace="ns",
        agent="a",
        memory_type="semantic",
        memory_key="user_home_city",
        new_value="Lisbon",
        new_embedding=[0.1, 0.2],
        candidate_rows=rows,
        resolved_threshold=0.85,
        resolved_policy="auto_reinforce",
    )
    # Only the allowlist read hit ClickHouse; no threshold lookups, no peer statement.
    assert len(ch.query_one_calls) == 0
    assert all("conflict_allowlist" in sql for sql, _ in ch.query_all_calls)
    assert res.verdict == "merge" and res.peers[0].memory_key == "user_city"


@pytest.mark.asyncio
async def test_link_value_updates_uses_one_statement_per_batch(monkeypatch) -> None:
    from datetime import datetime

    from server.memory import store as memory_repo
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_write_candidate_batch", 6)
    writes: list[list[str]] = []

    async def fake_write(
        org: str, ids: list[str], update: Any, *, ch: Any = None, **_kw: Any
    ) -> int:
        writes.append(ids)
        _kw["on_written"]([{"memory_id": ids[0], "memory_key": "k"}])
        return len(ids)

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", fake_write)
    ch = StubCH()
    ch.query_all_returns = [
        [
            {
                "qidx": 1,
                "memory_id": "p1",
                "memory_key": "user_running_personal_best",
                "value": "User's personal best for 5k is 25 minutes",
                "valid_at": datetime(2023, 5, 23),
                "created_at": datetime(2026, 1, 1),
                "cos": 0.82,
            },
        ]
    ]
    rows = [
        {
            "memory_id": "n0",
            "memory_key": "k0",
            "memory_type": "semantic",
            "value": "x",
            "valid_at": datetime(2023, 5, 30),
            "created_at": datetime(2026, 1, 2),
        },
        {
            "memory_id": "n1",
            "memory_key": "user_5k_personal_best_time",
            "memory_type": "semantic",
            "value": "User's personal best for 5k is 22 minutes",
            "valid_at": datetime(2023, 5, 30),
            "created_at": datetime(2026, 1, 2),
        },
    ]
    rep = await value_updates.link_value_updates(
        org="o",
        namespace="ns",
        rows=rows,
        embeddings_by_key={"k0": [0.1, 0.2], "user_5k_personal_best_time": [0.3, 0.4]},
        agent="a",
        ch=ch,
    )
    assert len(ch.query_all_calls) == 1  # two rows, one statement
    _, params = ch.query_all_calls[0]
    assert params["keys"] == ["k0", "user_5k_personal_best_time"] and params[
        "min_cos"
    ] == pytest.approx(0.75)
    assert rep.checked == 2 and len(rep.links) == 1 and writes == [["p1"]]


@pytest.mark.asyncio
async def test_batch_zero_keeps_the_per_row_statement(monkeypatch) -> None:
    from datetime import datetime

    from server.settings import settings

    monkeypatch.setattr(settings, "memory_write_candidate_batch", 0)
    ch = StubCH()
    rows = [
        {
            "memory_id": f"n{i}",
            "memory_key": f"k{i}",
            "memory_type": "semantic",
            "value": "x",
            "valid_at": datetime(2023, 5, 30),
            "created_at": datetime(2026, 1, 2),
        }
        for i in range(3)
    ]
    await value_updates.link_value_updates(
        org="o",
        namespace="ns",
        rows=rows,
        embeddings_by_key={f"k{i}": [0.1, 0.2] for i in range(3)},
        agent="a",
        ch=ch,
    )
    assert len(ch.query_all_calls) == 3
    assert all("qidx" not in sql for sql, _ in ch.query_all_calls)


@pytest.mark.asyncio
async def test_no_parameter_of_a_full_batch_reaches_the_128_kib_field_cap() -> None:
    # Six 3072-d vectors (the bulk default batch, production width) in one array
    # parameter are ~400 KB as text; ClickHouse refuses any field over 128 KiB.
    from clickhouse_connect.driver.binding import format_query_value

    ch = StubCH()
    vec = [0.123456789] * 3072
    items = [("semantic", f"k{i}", vec, 0.3) for i in range(6)]
    await conflicts.candidate_rows_many(org="o", namespace="ns", items=items, batch=6, ch=ch)
    ((sql, params),) = ch.query_all_calls
    sizes = {k: len(str(format_query_value(v))) for k, v in params.items()}
    assert max(sizes.values()) < 128 * 1024, sizes
    assert sum(sizes.values()) > 128 * 1024, "the batch itself is past one field's cap"
    assert all(f"{{emb{i}:Array(Float32)}}" in sql for i in range(1, 7))
