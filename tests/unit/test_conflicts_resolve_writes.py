"""What conflicts.resolve writes: only rows that await a decision, and no tombstone of a
locked memory. Both are judged on the heads a resolve reads first, before its first
write: one that answers `resolved: false` or is refused there leaves every row as it was.

A resolve decides a memory the Review queue lists: flagged `conflicted`, and not held
for a proposal's review. On any other target it resolves nothing. `contract` does not
tombstone an ordinary fact, a settled pair is not decided again, and a `revise` on a
proposal does not tombstone the facts it contradicts.

The same holds for the peers. A row keeps its `conflict_with` once its conflict is
settled, so a flagged target names peers settled earlier beside the ones flagged now. A
peer that is not flagged is neither tombstoned nor reactivated: one settled with `allow`
survives a later `revise`, and a proposal stays on trial.

The states are read current, from the rows' heads, and each write judges again the head
it reads: a row another caller settles in between is left as it is. A resolve that fails
between its two writes leaves the target flagged, so a retry completes it.

A lock refuses `memory.delete` (423 LOCKED), and a resolve refuses the same row. It
tombstones the side that loses, so it reads those heads current and asserts their locks.
`allow` tombstones nothing and settles a locked pair, and a lapsed lock is no lock.

The handler, `conflicts_repo.tombstone_ids` and `write_versions_by_ids` are the real
ones. The reads and the two inserts are faked, as in test_tombstone_cascade.py; the faked
insert keeps the proposal fence every real insert passes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory import store as memory_repo
from server.memory.api.models_conflicts import ConflictsResolveInput
from server.memory.app import conflicts_ops as h
from server.memory.domain import lock
from server.memory.domain.proposal_review import enforce_review
from server.memory.store import conflicts as conflicts_repo
from server.memory.store import lifecycle

NEW = "11111111-1111-1111-1111-111111111111"
OLD = "22222222-2222-2222-2222-222222222222"
DRAFT = "33333333-3333-3333-3333-333333333333"

DECISIONS = ["revise", "contract", "allow"]


def _row(mid: str, key: str, *peers: str, status: str = "conflicted") -> memory_repo.MemoryRow:
    return memory_repo.MemoryRow(
        memory_id=mid,
        org="acme",
        namespace="fleet-a",
        agent="a",
        user_id="-",
        session_id="s1",
        memory_key=key,
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 1),
        updated_at=datetime(2026, 9, 1),
        expires_at=None,
        source_trace="",
        status=status,
        requires_review=status == "proposed",
        conflict_with=list(peers),
    )


@dataclass
class _Store:
    """The namespace's rows, and what a resolve read and wrote. A written version
    becomes the row's head, as on the real store."""

    rows: dict[str, memory_repo.MemoryRow]
    written: list[dict[str, Any]] = field(default_factory=list)
    allowlisted: list[tuple[str, str]] = field(default_factory=list)
    strong_reads: list[bool] = field(default_factory=list)
    # Runs once the resolve has read its target and peers: another caller's write.
    after_first_reads: Callable[[], None] | None = None
    # The inserts that fail, counted from 1; the allowlist's insert is not one of them.
    failing_inserts: set[int] = field(default_factory=set)
    inserts: int = 0
    allowlist_fails: bool = False

    def change(self, mid: str, **update: Any) -> None:
        self.rows[mid] = self.rows[mid].model_copy(update=update)

    def statuses(self) -> dict[str, str]:
        return {str(r["memory_id"]): r["status"] for r in self.written}

    def live(self) -> dict[str, str]:
        return {m: r.status for m, r in self.rows.items() if r.tombstoned_at is None}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    """A conflicted pair: NEW was flagged against OLD."""
    s = _Store({NEW: _row(NEW, "deploy_day_new", OLD), OLD: _row(OLD, "deploy_day", NEW)})

    async def get_by_id(mid: str, **kw: Any) -> Any:
        if kw.get("include_tombstoned"):
            raise AssertionError("a resolve reads live heads only")
        s.strong_reads.append(bool(kw.get("strong")))
        row = s.rows.get(mid)
        found = row is not None and row.namespace == kw.get("namespace")
        answer = row if found and row.tombstoned_at is None else None
        if len(s.strong_reads) == 2 and s.after_first_reads is not None:
            s.after_first_reads()
        return answer

    async def insert(built: list[dict[str, Any]], **callbacks: Any) -> None:
        enforce_review(built, s.rows)
        s.inserts += 1
        if s.inserts in s.failing_inserts:
            raise ConnectionError("insert failed")
        s.written.extend(built)
        for version in built:
            mid = str(version["memory_id"])
            s.change(mid, status=version["status"], tombstoned_at=version["tombstoned_at"])
        if on_inserted := callbacks.get("on_inserted"):
            on_inserted()

    async def mirror(built: list[dict[str, Any]], **_: Any) -> int:
        return len(built)

    async def count(*_: Any, **__: Any) -> int:
        return 0

    async def allowlist(pairs: list[dict[str, Any]], **_: Any) -> None:
        if s.allowlist_fails:
            raise ConnectionError("allowlist insert failed")
        s.allowlisted.extend((p["memory_key_a"], p["memory_key_b"]) for p in pairs)

    async def announce(events: Any) -> None:
        return None

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr("server.memory.store.get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", mirror)
    monkeypatch.setattr(lifecycle, "_default_client", lambda: object())
    monkeypatch.setattr(conflicts_repo, "count_conflicted", count)
    monkeypatch.setattr(conflicts_repo, "allowlist_insert", allowlist)
    monkeypatch.setattr(h.webhooks, "announce", announce)
    h.reset_conflicts_count_cache()
    return s


def _lock(store: _Store, mode: str, *, until: datetime | None = None) -> None:
    """Locks the older row."""
    store.change(OLD, lock_mode=mode, lock_until=until, lock_reason="keep", locked_by="curator")
    assert lock.refuses(store.rows[OLD], "delete") is (bool(mode) and until is None)


async def _resolve(memory_id: str, decision: str, peer_id: str | None = None) -> Any:
    return await h.handle_conflicts_resolve(
        ConflictsResolveInput(
            org="acme",
            namespace="fleet-a",
            memory_id=memory_id,
            decision=decision,  # type: ignore[arg-type]
            peer_id=peer_id,
        )
    )


# ---- The target: only an unresolved conflict is decided ----


@pytest.mark.parametrize("decision", DECISIONS)
async def test_a_proposal_awaiting_review_resolves_nothing(store: _Store, decision: str) -> None:
    # A proposal records the facts it contradicts and keeps its status; they stay active.
    store.rows[NEW] = _row(NEW, "deploy_day_new", OLD, status="proposed")
    store.change(OLD, status="active", conflict_with=[])
    out = await _resolve(NEW, decision)
    assert (out.resolved, out.affected_memory_ids, out.resolved_at) == (False, [], None)
    assert store.written == []


@pytest.mark.parametrize("decision", DECISIONS)
async def test_a_flagged_memory_held_for_review_resolves_nothing(
    store: _Store, decision: str
) -> None:
    # The fence serves no row held for review, whatever its status says.
    store.change(NEW, requires_review=True)
    out = await _resolve(NEW, decision)
    assert (out.resolved, out.affected_memory_ids) == (False, [])
    assert store.written == []


@pytest.mark.parametrize("decision", DECISIONS)
@pytest.mark.parametrize("peer", ["settled", "flagged-again", "none"])
async def test_a_memory_that_is_not_flagged_resolves_nothing(
    store: _Store, decision: str, peer: str
) -> None:
    # A pair settled with `allow` is active and keeps its `conflict_with`; its other
    # side may be flagged again, in a conflict with a third row. An ordinary fact names
    # no peer.
    links = peer != "none"
    store.change(NEW, status="active", conflict_with=[OLD] if links else [])
    store.change(
        OLD,
        status="conflicted" if peer == "flagged-again" else "active",
        conflict_with=[NEW] if links else [],
    )
    out = await _resolve(NEW, decision)
    assert (out.resolved, out.affected_memory_ids) == (False, [])
    assert store.written == []


# ---- The peers: only the flagged ones are written ----


@pytest.mark.parametrize(
    ("decision", "wrote"),
    [
        ("revise", {DRAFT: "tombstoned", NEW: "active"}),
        ("contract", {NEW: "tombstoned", DRAFT: "active"}),
        ("allow", {NEW: "active", DRAFT: "active"}),
    ],
    ids=DECISIONS,
)
async def test_a_peer_settled_earlier_is_left_as_it_is(
    store: _Store, decision: str, wrote: dict[str, str]
) -> None:
    # NEW and OLD were settled with `allow` and still name each other; DRAFT was written
    # since and flagged against NEW. Deciding NEW decides it against DRAFT only.
    store.change(NEW, conflict_with=[OLD, DRAFT])
    store.change(OLD, status="active")
    store.rows[DRAFT] = _row(DRAFT, "deploy_day_draft", NEW)
    out = await _resolve(NEW, decision)
    assert out.resolved and sorted(out.affected_memory_ids) == sorted(wrote)
    assert store.statuses() == wrote  # OLD is not tombstoned by `revise`, nor rewritten


@pytest.mark.parametrize(
    ("decision", "wrote"),
    [("revise", {NEW: "active"}), ("contract", {NEW: "tombstoned"}), ("allow", {NEW: "active"})],
    ids=DECISIONS,
)
async def test_a_peer_on_trial_stays_on_trial(
    store: _Store, decision: str, wrote: dict[str, str]
) -> None:
    store.change(NEW, conflict_with=[DRAFT])
    store.rows[DRAFT] = _row(DRAFT, "deploy_day_draft", NEW, status="proposed")
    out = await _resolve(NEW, decision)
    assert (out.resolved, out.affected_memory_ids) == (True, [NEW])
    assert store.statuses() == wrote  # the proposal is not written, so nothing is refused
    # `allow` still keeps the pair from being flagged again once the proposal is served.
    assert store.allowlisted == ([("deploy_day_draft", "deploy_day_new")] * (decision == "allow"))


async def test_a_named_peer_narrows_the_resolve_to_that_peer(store: _Store) -> None:
    store.change(NEW, conflict_with=[OLD, DRAFT])
    store.rows[DRAFT] = _row(DRAFT, "deploy_day_draft", NEW)
    out = await _resolve(NEW, "revise", peer_id=DRAFT)
    assert out.resolved and store.statuses() == {DRAFT: "tombstoned", NEW: "active"}


async def test_every_head_a_resolve_judges_is_read_current(store: _Store) -> None:
    for decision in DECISIONS:
        store.rows[NEW], store.rows[OLD] = _row(NEW, "k_new", OLD), _row(OLD, "k", NEW)
        assert (await _resolve(NEW, decision)).resolved
    assert store.strong_reads and all(store.strong_reads)


# ---- Each write judges the head it reads ----


@pytest.mark.parametrize(
    ("decision", "settled", "wrote"),
    [
        ("revise", OLD, {NEW: "active"}),
        ("contract", OLD, {NEW: "tombstoned"}),
        ("allow", OLD, {NEW: "active"}),
        ("revise", NEW, {OLD: "tombstoned"}),
        ("contract", NEW, {OLD: "active"}),
        ("allow", NEW, {OLD: "active"}),
    ],
    ids=[f"{d}-{side}" for side in ("peer", "target") for d in DECISIONS],
)
async def test_a_row_settled_between_the_reads_is_left_as_it_is(
    store: _Store, decision: str, settled: str, wrote: dict[str, str]
) -> None:
    # Another caller settles one side once this resolve has read both: it is neither
    # tombstoned nor rewritten, and the answer does not list it.
    store.after_first_reads = lambda: store.change(settled, status="active")
    out = await _resolve(NEW, decision)
    assert (out.resolved, out.affected_memory_ids) == (True, list(wrote))
    assert store.statuses() == wrote


@pytest.mark.parametrize("decision", DECISIONS)
async def test_a_pair_settled_between_the_reads_resolves_nothing(
    store: _Store, decision: str
) -> None:
    def settle() -> None:
        store.change(NEW, status="active")
        store.change(OLD, status="active")

    store.after_first_reads = settle
    out = await _resolve(NEW, decision)
    assert (out.resolved, out.affected_memory_ids, out.resolved_at) == (False, [], None)
    assert store.written == []


# ---- A failure between the two writes ----


@pytest.mark.parametrize(
    ("decision", "fails", "after_failure", "after_retry"),
    [
        ("revise", 1, {NEW: "conflicted", OLD: "conflicted"}, {NEW: "active"}),
        ("revise", 2, {NEW: "conflicted"}, {NEW: "active"}),
        ("contract", 1, {NEW: "conflicted", OLD: "conflicted"}, {OLD: "active"}),
        ("contract", 2, {NEW: "conflicted", OLD: "active"}, {OLD: "active"}),
        ("allow", 1, {NEW: "conflicted", OLD: "conflicted"}, {NEW: "active", OLD: "active"}),
    ],
    ids=["revise-1", "revise-2", "contract-1", "contract-2", "allow-1"],
)
async def test_a_failed_write_leaves_the_target_flagged_and_a_retry_completes(
    store: _Store,
    decision: str,
    fails: int,
    after_failure: dict[str, str],
    after_retry: dict[str, str],
) -> None:
    store.failing_inserts = {fails}
    with pytest.raises(ConnectionError):
        await _resolve(NEW, decision)
    assert store.live() == after_failure  # the target is flagged still
    assert (await _resolve(NEW, decision)).resolved
    assert store.live() == after_retry


async def test_an_allow_that_fails_at_the_allowlist_writes_nothing_and_a_retry_completes(
    store: _Store,
) -> None:
    store.allowlist_fails = True
    with pytest.raises(ConnectionError):
        await _resolve(NEW, "allow")
    assert store.written == []  # still flagged, so the retry is not answered `resolved: false`
    store.allowlist_fails = False
    out = await _resolve(NEW, "allow")
    assert out.resolved and store.statuses() == {NEW: "active", OLD: "active"}
    assert store.allowlisted == [("deploy_day", "deploy_day_new")]


# ---- A lock refuses the tombstone ----


@pytest.mark.parametrize("mode", ["read_only", "no_delete"])
@pytest.mark.parametrize(
    ("memory_id", "decision"), [(NEW, "revise"), (OLD, "contract")], ids=["revise", "contract"]
)
async def test_a_resolve_that_would_tombstone_a_locked_row_is_refused(
    store: _Store, mode: str, memory_id: str, decision: str
) -> None:
    _lock(store, mode)
    with pytest.raises(HTTPException) as refused:
        await _resolve(memory_id, decision)
    assert refused.value.status_code == 423
    assert refused.value.detail["code"] == "LOCKED"
    assert refused.value.detail["memory_key"] == "deploy_day"
    assert store.written == []  # neither the tombstone nor the kept side's status


async def test_one_locked_peer_refuses_the_whole_resolve(store: _Store) -> None:
    _lock(store, "no_delete")
    store.rows[DRAFT] = _row(DRAFT, "deploy_day_draft", NEW)  # unlocked, and read first
    store.change(NEW, conflict_with=[DRAFT, OLD])
    with pytest.raises(HTTPException) as refused:
        await _resolve(NEW, "revise")
    assert refused.value.status_code == 423
    assert store.written == []


async def test_a_lapsed_lock_is_no_lock(store: _Store) -> None:
    _lock(store, "read_only", until=datetime(2026, 9, 2))
    out = await _resolve(NEW, "revise")
    assert out.resolved
    assert store.statuses() == {OLD: "tombstoned", NEW: "active"}


async def test_allow_settles_a_locked_pair(store: _Store) -> None:
    _lock(store, "read_only")
    out = await _resolve(NEW, "allow")
    assert out.resolved
    assert store.statuses() == {NEW: "active", OLD: "active"}


async def test_an_unlocked_pair_resolves(store: _Store) -> None:
    out = await _resolve(NEW, "revise")
    assert out.resolved
    assert store.statuses() == {OLD: "tombstoned", NEW: "active"}
