"""Unit tests for the worker-side repository helpers.

These cover the SELECT/UPDATE/INSERT shapes the surviving background
workers (embedding, entity_extractor, erasure_recovery,
audit_verifier) issue. None of them touch a
live ClickHouse — every test mocks the _CHClient Protocol via a stub.

The point is the same as the handler-side repo tests: lock in the
SQL shape so a refactor that drops a WHERE clause or mis-orders an
INSERT column fails the suite loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.erasure.store import ledger as erasure_ledger
from server.insights.store import audit as audit_repo
from server.memory import store as memory_repo


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


# ----------------------------------------------------------------------
# memory_repo: worker-side helpers
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_embedding_stubs_filters_and_bounds_by_attempts() -> None:
    """embedding_worker — must exclude already-poisoned rows
    (embed_failed_at IS NULL) and respect max_attempts."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await memory_repo.find_embedding_stubs(
        batch_size=100,
        max_attempts=5,
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "embedding_dim = 0" in sql
    assert "tombstoned_at IS NULL" in sql
    assert "embed_failed_at IS NULL" in sql
    assert "embed_attempts < {ma:UInt16}" in sql
    assert params["ma"] == 5
    assert params["lim"] == 100


@pytest.mark.asyncio
async def test_find_unentitied_memories_filters_searchable_types() -> None:
    """Entity extraction is only meaningful for semantic/procedural/
    preference memories. Episodic is session-scoped and never extracted.
    The repo uses argMax(empty(entities), version) in HAVING — no FINAL,
    relies on argMax to read latest-version state per memory_key."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await memory_repo.find_unentitied_memories(batch_size=25, ch=ch)
    sql, _ = ch.query_all_calls[0]
    # Column reference is `m.entities` under the JOIN alias; the tombstoned
    # check is a separate HAVING clause — both signals must agree on
    # "latest version".
    assert "argMax(empty(m.entities), version) = 1" in sql
    assert "argMax(m.tombstoned_at IS NULL, version) = 1" in sql
    assert "memory_type IN ('semantic', 'procedural', 'preference')" in sql


# ----------------------------------------------------------------------
# audit_repo: verifier helper
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_window_partition_alias_for_grouping() -> None:
    """audit_verifier groups break-detection by partition; the SELECT
    must alias `toYYYYMM(ts) AS partition` so the worker reads it back."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await audit_repo.verify_window(3600, ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "toYYYYMM(ts) AS partition" in sql
    assert "ORDER BY ts, request_id, row_hash" in sql and "LIMIT {lim:UInt32}" in sql


# ----------------------------------------------------------------------
# erasure ledger: stuck-saga scan + verify_window guard
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stuck_in_progress_attempts_budget() -> None:
    """Recovery worker must STOP retrying past max_attempts. The WHERE
    filter encodes that — without it, the worker loops forever. It also
    resumes `failed` sagas (a backend that fail-closed on its last attempt),
    not just `in_progress` ones, so a transient backend outage self-heals
    instead of stranding the erasure."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await erasure_ledger.list_stuck_in_progress(
        stale_threshold_seconds=300,
        max_attempts=5,
        batch_size=10,
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "status IN ('in_progress', 'failed')" in sql
    assert "attempts < {ma:UInt16}" in sql
    assert "last_checkpoint_at < now() - INTERVAL {st:UInt32}" in sql
    assert params["ma"] == 5
    assert params["bs"] == 10


@pytest.mark.asyncio
async def test_find_embedding_stubs_is_two_phase_and_skips_final_when_empty() -> None:
    """Phase 1 is a FINAL-free PREWHERE candidate scan; with no
    candidates the FINAL read is never issued (it can read GBs per pass to
    find an empty queue)."""
    ch = StubCH()
    ch.query_all_returns.append([])
    rows = await memory_repo.find_embedding_stubs(batch_size=100, max_attempts=5, ch=ch)
    assert rows == []
    assert len(ch.query_all_calls) == 1
    sql, params = ch.query_all_calls[0]
    assert "FINAL" not in sql
    assert "PREWHERE embedding_dim = 0" in sql
    assert params["cand_lim"] == 400
    # newest stub first: stale unmerged versions cannot crowd out a fresh stub
    assert "ORDER BY max(version) DESC" in sql


@pytest.mark.asyncio
async def test_find_embedding_stubs_prunes_the_final_read_to_candidate_keys() -> None:
    ch = StubCH()
    cand = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "s",
        "scope": "private",
        "embedding_model": "m",
        "memory_key": "k1",
    }
    ch.query_all_returns.append([cand])  # phase 1: PREWHERE candidates
    ch.query_all_returns.append([cand])  # phase 1b: confirmed on the newest version
    ch.query_all_returns.append([{**cand, "embedding_dim": 0, "version": 7}])  # phase 2
    rows = await memory_repo.find_embedding_stubs(
        batch_size=50, max_attempts=3, ch=ch, org_filter="o"
    )
    assert len(rows) == 1 and rows[0]["version"] == 7
    assert len(ch.query_all_calls) == 3
    sql1b, params1b = ch.query_all_calls[1]
    assert "FINAL" not in sql1b and "argMax(embedding_dim, version) = 0" in sql1b
    # argMax skips NULLs, so liveness is tested inside it: an older tombstoned row (a
    # high write's fence) must not hide the newer live stub above it
    assert "argMax(tombstoned_at IS NULL, version) = 1" in sql1b
    assert "argMax(embed_failed_at IS NULL, version) = 1" in sql1b
    assert params1b["keys"] == [("o", "n", "a", "-", "s", "private", "m", "k1")]
    sql, params = ch.query_all_calls[2]
    assert "FINAL" in sql and "IN {keys:Array(Tuple(" in sql
    assert params["keys"] == [("o", "n", "a", "-", "s", "private", "m", "k1")]
    assert params["ma"] == 3 and params["lim"] == 50
    assert "org = {org_filter:String}" in sql and params["org_filter"] == "o"


@pytest.mark.asyncio
async def test_find_embedding_stubs_stops_when_no_candidate_is_a_live_stub() -> None:
    """A stub row that was later embedded keeps its stub version until a merge;
    phase 1 returns it, the confirmation rejects it, and the
    FINAL read — every column of its granules — never runs."""
    ch = StubCH()
    cand = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "s",
        "scope": "private",
        "embedding_model": "m",
        "memory_key": "k1",
    }
    ch.query_all_returns.append([cand])
    ch.query_all_returns.append([])  # newest version is not a stub
    rows = await memory_repo.find_embedding_stubs(batch_size=50, max_attempts=3, ch=ch)
    assert rows == []
    assert len(ch.query_all_calls) == 2


_STUB_COLS = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "scope",
    "embedding_model",
    "memory_key",
)


class _KeyedCH(StubCH):
    """Answers each read of the stub selection from the keys it binds, ordered and
    cut as ClickHouse would: the candidates, all confirmed, and the FINAL read's
    rows, held groups last, then the oldest first."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        super().__init__()
        self.candidates = candidates

    async def query_all(self, sql, params=None):
        params = params or {}
        self.query_all_calls.append((sql, params))
        if "PREWHERE embedding_dim = 0" in sql:
            return self.candidates[: params["cand_lim"]]
        rows = [dict(zip(_STUB_COLS, key, strict=True)) for key in params["keys"]]
        if "HAVING" in sql:
            return rows
        held = set(params.get("held") or [])
        for r in rows:
            r["created_at"] = int(r["memory_key"].split("_")[2])  # the key's number
            r["version"] = 1
        rows.sort(
            key=lambda r: (
                (r["org"], r["namespace"], r["agent"], r["user_id"]) in held,
                r["created_at"],
            )
        )
        return rows[: params["lim"]]


@pytest.mark.asyncio
async def test_a_large_stub_selection_binds_every_list_in_parameters_that_fit() -> None:
    """Sixteen hundred cell-shaped candidate keys (a batch of 400) go to the server in
    parameters that each fit one form field, all of them read, and the batch is the
    oldest stubs."""
    import uuid

    from clickhouse_connect.driver.binding import format_bind_value

    from server.core.ch_params import FIELD_BYTES

    def cell(i: int) -> dict[str, Any]:
        return dict(
            zip(
                _STUB_COLS,
                (
                    str(uuid.UUID(int=7)),
                    f"cell-{uuid.UUID(int=i % 9)}-{uuid.UUID(int=i % 9 + 1)}",
                    f"agent-{i % 9}",
                    str(uuid.UUID(int=99)),
                    str(uuid.UUID(int=i + 4)),
                    "private",
                    "text-embedding-3-small",
                    f"user_preference_{i:06d}_" + "x" * 24,
                ),
                strict=True,
            )
        )

    ch = _KeyedCH([cell(i) for i in range(1600)])
    rows = await memory_repo.find_embedding_stubs(batch_size=400, max_attempts=3, ch=ch)
    for _, params in ch.query_all_calls:
        for value in params.values():
            if isinstance(value, list):
                assert len(format_bind_value(value)) <= FIELD_BYTES
    assert (
        sum(len(p["keys"]) for s, p in ch.query_all_calls if "keys" in p and "HAVING" in s) == 1600
    )
    assert len(ch.query_all_calls) > 3  # the keys went in several reads
    assert len(rows) == 400
    numbers = [r["created_at"] for r in rows]
    assert numbers == sorted(numbers) and numbers[0] == 0  # the oldest first
    phase1 = ch.query_all_calls[0][0]
    assert "argMax(embed_retry_at IS NULL OR embed_retry_at <= now64(3), version)" in phase1
