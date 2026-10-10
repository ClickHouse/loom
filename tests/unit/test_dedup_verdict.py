"""Unit tests for the dedup verdict tier in conflicts.check_on_commit.

The duplicate verdict sits above the existing conflict threshold:
  similarity ≥ duplicate_threshold (default 0.97) AND canonical-form
  differs → 'duplicate' (insert proceeds with status='duplicate'
  + duplicate_of pointer to canonical peer).

These tests pin the verdict-tier ordering: merge > duplicate >
rejected > conflict > clear. We stub the CH client to control the
similarity scores feeding the decision.
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeCH:
    """Minimal CH stub. Stages the embedding-search rows that
    check_on_commit will see + the namespaces-threshold row."""

    def __init__(
        self,
        peers: list[dict[str, Any]],
        *,
        conflict_threshold: float | None = None,
        duplicate_threshold: float | None = None,
        allowlist: list[dict[str, str]] | None = None,
    ) -> None:
        self.peers = peers
        self.conflict_threshold = conflict_threshold
        self.duplicate_threshold = duplicate_threshold
        self.allowlist = allowlist or []

    async def query_all(self, sql: str, params=None):
        # Stage A ranks the vectors, stage B reads the rows.
        if "loom.memory_embeddings" in sql or "{ids:Array(Tuple" in sql:
            return self.peers
        if "loom.conflict_allowlist" in sql:
            return self.allowlist
        return []

    async def query_one(self, sql: str, params=None):
        # The threshold-resolver SELECTs the column it cares about; we
        # have to honor the column name in the SELECT to route correctly.
        if "duplicate_threshold" in sql:
            return (
                None
                if self.duplicate_threshold is None
                else {
                    "duplicate_threshold": self.duplicate_threshold,
                }
            )
        if "conflict_threshold" in sql:
            return (
                None
                if self.conflict_threshold is None
                else {
                    "conflict_threshold": self.conflict_threshold,
                }
            )
        return None


@pytest.fixture
def install_ch(monkeypatch: pytest.MonkeyPatch):
    """Replace the CH client returned by get_client() with our stub."""

    def _install(ch: _FakeCH) -> None:
        monkeypatch.setattr("server.memory.app.conflicts.get_client", lambda: ch)

    return _install


@pytest.mark.asyncio
async def test_clear_when_no_peers(install_ch) -> None:
    from server.memory.app.conflicts import check_on_commit

    install_ch(_FakeCH(peers=[]))
    result = await check_on_commit(
        org="clickhouse",
        namespace="finance",
        agent="a",
        memory_type="semantic",
        memory_key="k1",
        new_value="hello",
        new_embedding=[0.1, 0.2],
    )
    assert result.verdict == "clear"
    assert result.peers == []


@pytest.mark.asyncio
async def test_merge_on_canonical_form_match(install_ch) -> None:
    """Canonical-form match short-circuits to merge regardless of
    distance (the existing path; just confirming we didn't break it)."""
    from server.memory.app.conflicts import check_on_commit

    install_ch(
        _FakeCH(
            peers=[
                {
                    "memory_id": "uuid-1",
                    "memory_key": "k_existing",
                    "value": '"User prefers Postgres."',
                    "dist": 0.01,
                    "pinned": 0,
                },
            ]
        )
    )
    result = await check_on_commit(
        org="clickhouse",
        namespace="finance",
        agent="a",
        memory_type="semantic",
        memory_key="k_new",
        new_value='"user prefers postgres"',
        new_embedding=[0.1, 0.2],
    )
    assert result.verdict == "merge"
    assert result.existing_memory_id == "uuid-1"


@pytest.mark.asyncio
async def test_duplicate_when_high_similarity_canonical_mismatch(install_ch) -> None:
    """The new tier: similarity ≥ 0.97, canonical-form differs."""
    from server.memory.app.conflicts import check_on_commit

    install_ch(
        _FakeCH(
            peers=[
                {
                    "memory_id": "uuid-canonical",
                    "memory_key": "k_existing",
                    "value": '"User likes the beach."',
                    "dist": 0.02,  # similarity = 0.98
                    "pinned": 0,
                },
            ]
        )
    )
    result = await check_on_commit(
        org="clickhouse",
        namespace="finance",
        agent="a",
        memory_type="semantic",
        memory_key="k_new",
        new_value='"This user enjoys spending time by the seashore."',
        new_embedding=[0.1, 0.2],
    )
    assert result.verdict == "duplicate"
    assert result.duplicate_of_id == "uuid-canonical"


@pytest.mark.asyncio
async def test_conflict_when_below_duplicate_threshold(install_ch) -> None:
    """0.92 ≤ similarity < 0.97 with canonical-form mismatch stays in
    the existing conflict tier."""
    from server.memory.app.conflicts import check_on_commit

    install_ch(
        _FakeCH(
            peers=[
                {
                    "memory_id": "uuid-peer",
                    "memory_key": "k_peer",
                    "value": '"User dislikes Postgres."',
                    "dist": 0.06,  # similarity = 0.94
                    "pinned": 0,
                },
            ]
        )
    )
    result = await check_on_commit(
        org="clickhouse",
        namespace="finance",
        agent="a",
        memory_type="semantic",
        memory_key="k_new",
        new_value='"User loves Postgres."',
        new_embedding=[0.1, 0.2],
    )
    assert result.verdict == "conflict"
    assert result.duplicate_of_id is None


@pytest.mark.asyncio
async def test_pinned_short_circuit_takes_priority_over_duplicate(install_ch) -> None:
    """Pinned-blocker rejection must fire before the duplicate-tier
    branch — a pinned peer that's close-but-different blocks the write
    even if a *non-pinned* peer is very-high-similarity."""
    from server.memory.app.conflicts import check_on_commit

    install_ch(
        _FakeCH(
            peers=[
                {
                    "memory_id": "uuid-pinned",
                    "memory_key": "k_pinned",
                    "value": '"Pinned and different opinion."',
                    "dist": 0.04,
                    "pinned": 1,
                },
            ]
        )
    )
    result = await check_on_commit(
        org="clickhouse",
        namespace="finance",
        agent="a",
        memory_type="semantic",
        memory_key="k_new",
        new_value='"Restating my view another way."',
        new_embedding=[0.1, 0.2],
    )
    assert result.verdict == "rejected"
    assert result.pinned_blocker_id == "uuid-pinned"


@pytest.mark.asyncio
async def test_per_namespace_override_lowers_duplicate_threshold(install_ch) -> None:
    """`loom.namespaces.duplicate_threshold` override demotes the tier
    boundary — a 0.95 similarity hit becomes 'duplicate' for a tenant
    that explicitly opted into the wider band."""
    from server.memory.app.conflicts import check_on_commit

    install_ch(
        _FakeCH(
            peers=[
                {
                    "memory_id": "uuid-canonical",
                    "memory_key": "k_existing",
                    "value": '"some value"',
                    "dist": 0.05,  # similarity = 0.95
                    "pinned": 0,
                },
            ],
            duplicate_threshold=0.94,  # tenant override
        )
    )
    result = await check_on_commit(
        org="clickhouse",
        namespace="finance",
        agent="a",
        memory_type="semantic",
        memory_key="k_new",
        new_value='"some other phrasing entirely"',
        new_embedding=[0.1, 0.2],
    )
    assert result.verdict == "duplicate"


@pytest.mark.asyncio
async def test_memory_set_output_carries_duplicate_of() -> None:
    """The wire output's new field exists and defaults to empty."""
    from server.memory.api.models_crud import MemorySetOutput

    out = MemorySetOutput(version=1, memory_id="x", content_sha256="y")
    assert out.duplicate_of == ""
    out2 = MemorySetOutput(
        version=2,
        memory_id="x",
        content_sha256="y",
        duplicate_of="canonical-uuid",
    )
    assert out2.duplicate_of == "canonical-uuid"


# ----------------------------------------------------------------------
# A run's write keeps its own row
# ----------------------------------------------------------------------


def _restated_peer(run_id: str = "-") -> list[dict[str, Any]]:
    return [
        {
            "memory_id": "uuid-1",
            "memory_key": "k_existing",
            "value": '"User prefers Postgres."',
            "dist": 0.01,
            "pinned": 0,
            "run_id": run_id,
        },
    ]


async def _write(run_id: str):
    from server.memory.app.conflicts import check_on_commit

    return await check_on_commit(
        org="clickhouse",
        namespace="finance",
        agent="a",
        memory_type="semantic",
        memory_key="k_new",
        new_value='"user prefers postgres"',
        new_embedding=[0.1, 0.2],
        new_run_id=run_id,
    )


@pytest.mark.asyncio
async def test_run_scoped_restatement_of_another_scopes_row_is_a_duplicate(install_ch) -> None:
    """A run restating a row written outside the run (or by another run) gets
    its own row — status duplicate, pointing at the peer — so the run's
    run_id-scoped reads find what the run wrote (merged into the peer, the
    write would vanish under the run filter)."""
    install_ch(_FakeCH(peers=_restated_peer("-")))
    result = await _write("run-7")
    assert result.verdict == "duplicate"
    assert result.duplicate_of_id == "uuid-1"

    install_ch(_FakeCH(peers=_restated_peer("run-3")))
    result = await _write("run-7")
    assert result.verdict == "duplicate" and result.duplicate_of_id == "uuid-1"


@pytest.mark.asyncio
async def test_same_run_restatement_still_merges(install_ch) -> None:
    install_ch(_FakeCH(peers=_restated_peer("run-7")))
    result = await _write("run-7")
    assert result.verdict == "merge" and result.existing_memory_id == "uuid-1"


@pytest.mark.asyncio
async def test_unscoped_write_merges_regardless_of_the_peers_run(install_ch) -> None:
    install_ch(_FakeCH(peers=_restated_peer("run-3")))
    result = await _write("-")
    assert result.verdict == "merge"
    result = await _write("")
    assert result.verdict == "merge"
