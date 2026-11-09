"""The write path's peer reads take the two-stage, codes-first shape: stage A ranks
the namespace's vectors with a bare distance ORDER BY + LIMIT, which the Quantized
codec answers from its codes, and stage B reads the candidates' rows by sort key; the
full-width join is only the fallback past `_PEER_MAX_K` candidates."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import conflicts, value_updates


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


def _codes_first(sql: str) -> bool:
    """Stage A's shape: no join, the size guard that keeps the codes path, no full decode."""
    return "JOIN" not in sql and "embedding.size0" in sql and "length(embedding)" not in sql


@pytest.mark.asyncio
async def test_each_bulk_fact_reads_its_own_peers_codes_first(monkeypatch) -> None:
    from server.memory.app import bulk_set

    ch = StubCH()
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)
    entries = [
        SimpleNamespace(memory_type="semantic", memory_key=f"k{i}", valid_at=None) for i in range(3)
    ]
    put = SimpleNamespace(org="o", namespace="ns", agent="a", run_id="-", valid_at=None)
    out = await bulk_set._bulk_conflict_verdicts(
        put,
        [{}, {}, {}],
        [(e, f"value {i}", None, None, None) for i, e in enumerate(entries)],
        [(i, None, None, None) for i in range(3)],
        [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
    )
    assert set(out) == {0, 1, 2}  # every fact checked, none left unchecked by a failure
    peer_reads = [sql for sql, _ in ch.query_all_calls if "cosineDistance" in sql]
    assert len(peer_reads) == 3  # one stage A per fact; no candidate, no stage B
    assert all(_codes_first(sql) for sql in peer_reads)
    assert sorted(p["self_key"] for sql, p in ch.query_all_calls if "cosineDistance" in sql) == [
        "k0",
        "k1",
        "k2",
    ]


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
async def test_value_update_peers_are_read_codes_first_then_by_key(monkeypatch) -> None:
    from datetime import datetime

    from server.memory import store as memory_repo

    writes: list[list[str]] = []

    async def fake_write(
        org: str, ids: list[str], update: Any, *, ch: Any = None, **_kw: Any
    ) -> int:
        writes.append(ids)
        _kw["on_written"]([{"memory_id": ids[0], "memory_key": "k"}])
        return len(ids)

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", fake_write)
    ch = StubCH()
    peer = {
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "memory_key": "user_running_personal_best",
    }
    ch.query_all_returns = [
        [],  # k0: no candidate
        [{**peer, "dist": 0.18}],  # stage A for the second row
        [
            {
                **peer,
                "memory_id": "p1",
                "value": "User's personal best for 5k is 25 minutes",
                "pinned": 0,
                "run_id": "-",
                "valid_at": datetime(2023, 5, 23),
            }
        ],
    ]
    rows = [
        {
            "memory_id": "n0",
            "memory_key": "k0",
            "memory_type": "semantic",
            "value": "x",
            "valid_at": datetime(2023, 5, 30),
        },
        {
            "memory_id": "n1",
            "memory_key": "user_5k_personal_best_time",
            "memory_type": "semantic",
            "value": "User's personal best for 5k is 22 minutes",
            "valid_at": datetime(2023, 5, 30),
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
    stage_a = [sql for sql, _ in ch.query_all_calls if "LIMIT {k:UInt32}" in sql]
    assert len(stage_a) == 2  # one per row
    assert all(_codes_first(sql) for sql in stage_a)
    assert all(p["max_dist"] == pytest.approx(0.25) for _, p in ch.query_all_calls[:2])
    assert rep.checked == 2 and len(rep.links) == 1 and writes == [["p1"]]


def _bulk(entries: int) -> tuple[Any, list[Any], list[Any], list[list[float]]]:
    """`_bulk_conflict_verdicts`' arguments for `entries` semantic facts."""
    put = SimpleNamespace(org="o", namespace="n", agent="bot", run_id="-", valid_at=None)
    metas = [
        (
            SimpleNamespace(memory_type="semantic", memory_key=f"k{i}", valid_at=None),
            "U lives in Lisbon",
            None,
            None,
            None,
        )
        for i in range(entries)
    ]
    return put, metas, [(i, None, None, None) for i in range(entries)], [[0.1, 0.2]] * entries


def _no_reads(monkeypatch: pytest.MonkeyPatch, policy: str) -> list[str]:
    """The namespace's write configuration without ClickHouse: `policy`, a 0.85 conflict
    threshold, no allow-list. Returns the policy reads it served."""
    reads: list[str] = []

    async def read_policy(org: str, namespace: str, **_: Any) -> str:
        reads.append(namespace)
        return policy

    async def threshold(*_a: Any, **_k: Any) -> float:
        return 0.85

    async def no_allowlist(*_a: Any) -> set[str]:
        return set()

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", read_policy)
    monkeypatch.setattr(conflicts, "_resolve_threshold", threshold)
    monkeypatch.setattr(conflicts, "_resolve_duplicate_threshold", threshold)
    monkeypatch.setattr(conflicts, "_allowlisted_keys", no_allowlist)
    return reads


@pytest.mark.asyncio
async def test_an_append_only_bulk_write_clears_every_entry_without_a_peer_read(
    monkeypatch,
) -> None:
    """Under `append_only` every entry of a bulk write is its own row: the batch reads the
    policy once and no entry issues a peer statement, as for a single write."""
    from server.memory.app import bulk_set

    ch = StubCH()
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)
    reads = _no_reads(monkeypatch, "append_only")
    put, metas, pending, vectors = _bulk(3)
    out = await bulk_set._bulk_conflict_verdicts(put, [{}] * 3, metas, pending, vectors)
    assert [out[i].verdict for i in range(3)] == ["clear"] * 3
    assert reads == ["n"]  # read once for the batch
    assert not [sql for sql, _ in ch.query_all_calls if "cosineDistance" in sql]


def test_a_narrowed_bulk_writer_is_refused_by_another_users_pinned_peer(monkeypatch) -> None:
    """A writer bound to user U writes in bulk next to V's pinned row: the entry is
    rejected and V's row named as the blocker, never as a peer."""
    from server.memory.app import bulk_set
    from tests.unit.test_write_scope import _pinned, _PinnedV, _run_as

    ch = _PinnedV()
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)
    _no_reads(monkeypatch, "auto_reinforce")
    put, metas, pending, vectors = _bulk(1)
    out = _run_as(
        _pinned("U"),
        lambda: bulk_set._bulk_conflict_verdicts(put, [{}], metas, pending, vectors),
    )
    assert out[0].verdict == "rejected" and out[0].pinned_blocker_id == "v1"
    assert all(p.memory_id != "v1" for p in out[0].peers)
