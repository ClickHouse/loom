"""Bulk writes get memory.set's four conflict verdicts. The
fold from ConflictResults onto the rows about to be inserted is pure."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

from server.memory.app.bulk_verdicts import apply_bulk_verdicts
from server.memory.domain.conflicts import ConflictPeer, ConflictResult


def _rows(n: int):
    memory_rows = [
        {"memory_key": f"k{i}", "memory_id": f"id{i}", "status": "active"} for i in range(n)
    ]
    embedding_rows = [{"memory_key": f"k{i}", "status": "active"} for i in range(n)]
    emb_idx_of = list(range(n))
    results = [NS(memory_key=f"k{i}", version=100 + i, memory_id=f"id{i}") for i in range(n)]
    return memory_rows, embedding_rows, emb_idx_of, results


def _landing(rows: list[dict], *dropped: int) -> set[int]:
    """The `id()` of the rows that land: all but the `dropped` indexes."""
    return {id(r) for i, r in enumerate(rows) if i not in dropped}


def _stored(**keys: tuple[str, str]):
    """A bulk's state over stored rows `keys` (key -> (memory_id, sha)), no entries yet."""
    from server.memory.app.bulk_rows import _BulkWriteState

    state = _BulkWriteState(existing_lookup_ok=True)
    state.existing_ids = {k: mid for k, (mid, _sha) in keys.items()}
    state.existing_sha = {k: sha for k, (_mid, sha) in keys.items()}
    return state


def _entry(state, key: str, memory_id: str, sha: str, **labels: Any) -> None:
    """One entry (with row `labels` such as pinned or sensitivity) that passes the skip
    gate and builds a row."""
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app.bulk_rows import _bulk_skip_gate, _keep_row

    entry = NS(memory_key=key, **labels)
    assert not _bulk_skip_gate(entry, sha, state, {})
    result = _BulkSetResult(memory_key=key, version=1, memory_id=memory_id)
    state.entry_metas.append(None)
    state.n_updated += 1
    row = {"memory_key": key, "memory_id": memory_id, "content_sha256": sha, "pinned": False}
    level = labels.get("sensitivity", "")
    _keep_row(state, {**row, **labels}, entry, result, own_level=level, base_tags=[])


def _settle(state, verdicts: dict) -> set[int]:
    """Replay the batch and fold its `verdicts`; the indexes of the built rows the
    replay kept out."""
    from server.memory.app.bulk_rows import Outcome
    from server.memory.app.bulk_set import _apply_verdicts

    _apply_verdicts(state, [], verdicts)
    built = [s for s in state.steps if s.repeats is None]
    return {
        i
        for i, s in enumerate(built)
        if s.outcome in (Outcome.UNCHANGED, Outcome.PINNED, Outcome.LOCKED)
    }


def test_no_verdicts_is_a_pass_through() -> None:
    m, e, ix, r = _rows(3)
    plan = apply_bulk_verdicts(m, e, ix, r, {}, stale_rows=(), landed_rows=_landing(m))
    assert plan.memory_rows == m and plan.embedding_rows == e
    assert plan.unchecked == 0 and not plan.conflict_marks


def test_merge_and_rejected_drop_the_row_and_zero_its_result() -> None:
    m, e, ix, r = _rows(4)
    verdicts = {
        0: NS(verdict="merge", existing_memory_id="old0", peers=[]),
        2: NS(verdict="rejected", pinned_blocker_id="pin", peers=[]),
    }
    plan = apply_bulk_verdicts(m, e, ix, r, verdicts, stale_rows=(), landed_rows=_landing(m, 0, 2))
    assert [x["memory_key"] for x in plan.memory_rows] == ["k1", "k3"]
    assert [x["memory_key"] for x in plan.embedding_rows] == ["k1", "k3"]
    assert plan.merged == ["k0"] and plan.rejected_pinned == ["k2"]
    assert plan.reinforce_ids == ["old0"]
    assert r[0].version == 0 and r[2].version == 0 and r[1].version == 101
    # A dropped entry wrote no row, so its result names none.
    assert [x.memory_id for x in r] == ["", "id1", "", "id3"]


def test_duplicate_stamps_both_rows_and_conflict_records_peers() -> None:
    m, e, ix, r = _rows(3)
    verdicts = {
        1: NS(verdict="duplicate", duplicate_of_id="dup-of", peers=[]),
        2: NS(verdict="conflict", peers=[NS(memory_id="p1"), NS(memory_id="p2")]),
    }
    plan = apply_bulk_verdicts(m, e, ix, r, verdicts, stale_rows=(), landed_rows=_landing(m))
    assert len(plan.memory_rows) == 3
    assert (
        plan.memory_rows[1]["status"] == "duplicate"
        and plan.memory_rows[1]["duplicate_of"] == "dup-of"
    )
    assert plan.embedding_rows[1]["status"] == "duplicate"
    assert plan.duplicates == ["k1"] and plan.conflicted == ["k2"]
    assert plan.conflict_marks == [("id2", ["p1", "p2"])]
    assert r[1].version == 101  # duplicates are still written


def test_embedding_rows_follow_their_entry_not_their_position() -> None:
    # Entry 1 is not searchable (no embedding row); entries 0 and 2 are.
    m = [{"memory_key": f"k{i}", "memory_id": f"id{i}", "status": "active"} for i in range(3)]
    e = [{"memory_key": "k0"}, {"memory_key": "k2"}]
    ix = [0, 2]
    r = [NS(memory_key=f"k{i}", version=1) for i in range(3)]
    plan = apply_bulk_verdicts(
        m,
        e,
        ix,
        r,
        {2: NS(verdict="rejected", peers=[])},
        stale_rows=(),
        landed_rows=_landing(m, 2),
    )
    assert [x["memory_key"] for x in plan.memory_rows] == ["k0", "k1"]
    assert [x["memory_key"] for x in plan.embedding_rows] == ["k0"]


def test_unchecked_counts_searchable_rows_past_the_cap() -> None:
    m, e, ix, r = _rows(5)
    plan = apply_bulk_verdicts(
        m,
        e,
        ix,
        r,
        {0: NS(verdict="clear", peers=[]), 1: NS(verdict="clear", peers=[])},
        stale_rows=(),
        landed_rows=_landing(m),
    )
    assert plan.unchecked == 3


def test_every_row_carries_status_and_duplicate_of_after_a_duplicate_verdict() -> None:
    # Real rows do not carry `status` (the column has a default). ch.insert takes
    # the column list from rows[0], so a verdict stamped on ONE row fails the
    # batch with KeyError('status'). All rows must share one key set.
    m = [{"memory_key": f"k{i}", "memory_id": f"id{i}"} for i in range(3)]
    e = [{"memory_key": f"k{i}"} for i in range(3)]
    ix = list(range(3))
    r = [NS(memory_key=f"k{i}", version=1) for i in range(3)]
    plan = apply_bulk_verdicts(
        m,
        e,
        ix,
        r,
        {2: NS(verdict="duplicate", duplicate_of_id="dup", peers=[])},
        stale_rows=(),
        landed_rows=_landing(m),
    )
    key_sets = {frozenset(row.keys()) for row in plan.memory_rows}
    assert len(key_sets) == 1 and {"status", "duplicate_of"} <= next(iter(key_sets))
    assert plan.memory_rows[0]["status"] == "active" and plan.memory_rows[0]["duplicate_of"] == ""
    assert (
        plan.memory_rows[2]["status"] == "duplicate"
        and plan.memory_rows[2]["duplicate_of"] == "dup"
    )


def test_only_the_version_a_key_keeps_settles_its_verdict() -> None:
    # One key twice is two versions of one row: entry 0's verdict was on a value entry 1
    # replaced, so it marks and retires nothing. Entry 1 merged writes no row, and then
    # entry 0's value is the one kept.
    def rows():
        m = [
            {"memory_key": "alice", "memory_id": "a", "version": v, "value": f"v{v}"}
            for v in (1, 2)
        ]
        r = [NS(memory_key="alice", version=v, memory_id="a") for v in (1, 2)]
        return m, [], [], r

    peer = NS(memory_id="p", pinned=False)
    for verdict in ("conflict", "refines"):
        v = NS(verdict=verdict, peers=[peer], refined_peers=[peer])
        m, e, ix, r = rows()
        plan = apply_bulk_verdicts(m, e, ix, r, {0: v}, stale_rows=(), landed_rows=_landing(m))
        assert not plan.conflict_marks and not plan.refines
        assert not plan.conflicted and not plan.refined
        # Demoted to clear, it names no peers, as memory.set's demotion does.
        assert r[0].conflict_verdict == "clear" and r[0].peers == []
        m, e, ix, r = rows()
        plan = apply_bulk_verdicts(m, e, ix, r, {1: v}, stale_rows=(), landed_rows=_landing(m))
        assert len(plan.conflict_marks) + len(plan.refines) == 1
        m, e, ix, r = rows()
        merged = {0: v, 1: NS(verdict="merge", peers=[])}
        plan = apply_bulk_verdicts(m, e, ix, r, merged, stale_rows=(), landed_rows=_landing(m, 1))
        assert len(plan.conflict_marks) + len(plan.refines) == 1


def test_a_merge_with_a_row_an_earlier_entry_rewrote_lands() -> None:
    # Row 0 rewrites id0; rows 1 and 2 matched id0's stored bytes, which are gone. A
    # target rewritten only after the entry still merges, as memory.set in that order would.
    from server.memory.app.bulk_set import _apply_verdicts

    state = _stored(k0=("id0", "s0"))
    for i in range(3):
        _entry(state, f"k{i}", f"id{i}", f"n{i}")
    id0 = ConflictPeer(
        memory_id="id0", memory_key="k0", similarity=0.99, pinned=False, canonical=""
    )
    verdicts = {
        1: NS(verdict="merge", existing_memory_id="id0", peers=[id0]),
        2: NS(verdict="duplicate", duplicate_of_id="id0", peers=[id0]),
    }
    plan = _apply_verdicts(state, [], verdicts)
    assert [x["memory_key"] for x in plan.memory_rows] == ["k0", "k1", "k2"]
    assert plan.merged == [] and plan.duplicates == [] and plan.reinforce_ids == []
    assert state.results[1].conflict_verdict == "clear" and state.results[1].version == 1
    # The rows judged against are gone: a demoted verdict names none of them.
    assert state.results[1].peers == [] and state.results[2].peers == []
    state = _stored(k1=("id1", "s1"))
    _entry(state, "k0", "id0", "n0")
    _entry(state, "k1", "id1", "n1")
    late = _apply_verdicts(state, [], {0: NS(verdict="merge", existing_memory_id="id1")})
    assert late.merged == ["k0"]


def test_a_refine_or_conflict_with_a_row_another_entry_rewrites_lands() -> None:
    # Row 0 rewrites id0 ("User owns a red bicycle."); row 1 refined or contradicted
    # id0's old bytes ("User lives in Berlin."). Retiring or flagging id0 would hit the
    # bicycle. The same holds when the rewrite comes later in the batch: the marks run
    # after the insert, on the new value. A peer the batch did not rewrite still counts.
    def peer(mid: str) -> ConflictPeer:
        return ConflictPeer(
            memory_id=mid, memory_key=mid, similarity=0.9, pinned=False, canonical=""
        )

    for verdict in ("refines", "conflict"):
        v = ConflictResult(verdict=verdict, peers=[peer("id0")], refined_peers=[peer("id0")])
        m, e, ix, r = _rows(2)
        plan = apply_bulk_verdicts(m, e, ix, r, {1: v}, stale_rows=(), landed_rows=_landing(m))
        assert not plan.refines and not plan.conflict_marks
        assert r[1].conflict_verdict == "clear" and r[1].peers == []
        later = ConflictResult(verdict=verdict, peers=[peer("id1")], refined_peers=[peer("id1")])
        m, e, ix, r = _rows(2)
        plan = apply_bulk_verdicts(m, e, ix, r, {0: later}, stale_rows=(), landed_rows=_landing(m))
        assert not plan.refines and not plan.conflict_marks
        assert r[0].conflict_verdict == "clear" and r[0].peers == []
        for dropped in ("rejected", "merge"):
            # The later row writing id1's key is dropped and never lands: id1 keeps the
            # value judged, so row 0 still refines or contradicts it.
            m, e, ix, r = _rows(2)
            verdicts = {0: later, 1: ConflictResult(dropped, [])}
            plan = apply_bulk_verdicts(
                m, e, ix, r, verdicts, stale_rows=(), landed_rows=_landing(m, 1)
            )
            assert r[0].conflict_verdict == verdict
            if verdict == "refines":
                assert [p.memory_id for p in plan.refines[0][2]] == ["id1"]
            else:
                assert plan.conflict_marks == [("id0", ["id1"])]
        both = [peer("id0"), peer("p")]
        v = ConflictResult(verdict=verdict, peers=both, refined_peers=both)
        m, e, ix, r = _rows(2)
        plan = apply_bulk_verdicts(m, e, ix, r, {1: v}, stale_rows=(), landed_rows=_landing(m))
        if verdict == "refines":
            assert [p.memory_id for p in plan.refines[0][2]] == ["p"]
        else:
            assert plan.conflict_marks == [("id1", ["p"])]


def _batch(stored_sha: str, *entries: tuple[str, bool]):
    """A bulk of key k's values `entries` ((sha, pinned) each) through the skip gate,
    over a stored row of value `stored_sha` (unpinned, id e1)."""
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app.bulk_rows import _bulk_skip_gate, _BulkWriteState, _keep_row

    state = _BulkWriteState(existing_lookup_ok=True)
    state.existing_ids, state.existing_sha = {"k": "e1"}, {"k": stored_sha}
    for sha, pinned in entries:
        if _bulk_skip_gate(NS(memory_key="k"), sha, state, {}):
            continue
        result = _BulkSetResult(memory_key="k", version=1, memory_id="e1")
        state.entry_metas.append(None)  # type: ignore[arg-type]
        state.n_updated += 1
        row = {"memory_key": "k", "memory_id": "e1", "content_sha256": sha, "pinned": pinned}
        _keep_row(state, row, NS(memory_key="k"), result, own_level="", base_tags=[])
    return state


def test_a_pin_from_a_landed_bulk_row_refuses_a_later_value() -> None:
    state = _batch("s0", ("s1", True), ("s2", False))
    assert _settle(state, {}) == {1}
    assert state.skipped_pinned == ["k"]
    assert [r.conflict_verdict for r in state.results] == ["", "pinned"]


def test_a_pinning_write_replaces_a_pinned_value() -> None:
    # memory.set lets a pinning write change a pinned value; so does a bulk, whether the
    # pin is stored or an earlier entry landed it.
    from server.memory.app.bulk_rows import _bulk_skip_gate

    state = _stored(k=("K1", "s0"))
    state.existing_pinned = {"k": True}
    assert not _bulk_skip_gate(NS(memory_key="k", pinned=True), "s1", state, {})
    assert state.skipped_pinned == []
    state = _batch("s0", ("s1", True), ("s2", True))
    assert _settle(state, {}) == set()
    assert state.skipped_pinned == []


def test_returning_to_the_stored_value_after_a_rejected_row_is_unchanged() -> None:
    # Serially the rejected value never lands, so "s0" again restates the stored row.
    state = _batch("s0", ("s1", False), ("s0", False))
    assert _settle(state, {0: NS(verdict="rejected", peers=[])}) == {1}
    assert state.results[1].version == 0
    assert state.skipped_unchanged == 1 and state.restated_ids == ["e1"]


def test_a_merge_stays_a_merge_when_the_earlier_rewrite_of_its_target_did_not_land() -> None:
    # Stored: K (id X, value "x") and Y (id Y, value "y"). Entry 0 sets K to "y" and
    # merges into Y, so K keeps "x". Entry 1 sets L to "x": serially it merges into X.
    from server.memory.app.bulk_set import _apply_verdicts

    state = _stored(K=("X", "x"), Y=("Y", "y"))
    _entry(state, "K", "X", "y")
    _entry(state, "L", "L1", "x")
    verdicts = {
        0: NS(verdict="merge", existing_memory_id="Y", peers=[]),
        1: NS(verdict="merge", existing_memory_id="X", peers=[]),
    }
    plan = _apply_verdicts(state, [], verdicts)
    assert plan.memory_rows == [] and plan.merged == ["K", "L"]
    assert [(r.version, r.conflict_verdict) for r in state.results] == [(0, "merge")] * 2
    assert plan.reinforce_ids == ["Y", "X"]


def test_a_restated_merge_or_rejection_is_listed_with_its_verdict() -> None:
    # Entry 0 merges into M (or is refused by a pin); entry 1 repeats its bytes and gets
    # the same verdict, so the output lists both entries under it.
    from server.memory.app.bulk_rows import _bulk_skip_gate
    from server.memory.app.bulk_set import _apply_verdicts

    for verdict in ("merge", "rejected"):
        state = _stored()
        _entry(state, "k", "id1", "s1")
        assert _bulk_skip_gate(NS(memory_key="k"), "s1", state, {})
        plan = _apply_verdicts(state, [], {0: NS(verdict=verdict, existing_memory_id="M")})
        assert [r.conflict_verdict for r in state.results] == [verdict] * 2
        listed = plan.merged if verdict == "merge" else plan.rejected_pinned
        assert listed == ["k", "k"] and state.skipped_unchanged == 0


def test_a_restated_merge_before_its_target_is_rewritten_stays_a_merge() -> None:
    # Stored: t (T, Berlin). Entries: a=Berlin (merges into T), a=Berlin, t=bicycle,
    # a=Berlin. Serially the second a merges into T again; only the last a meets the
    # bicycle and lands, as the first a's row.
    from server.memory.app.bulk_rows import _bulk_skip_gate
    from server.memory.app.bulk_set import _apply_verdicts

    state = _stored(t=("T", "berlin"))
    _entry(state, "a", "A1", "berlin")
    assert _bulk_skip_gate(NS(memory_key="a"), "berlin", state, {})
    _entry(state, "t", "T", "bike")
    assert _bulk_skip_gate(NS(memory_key="a"), "berlin", state, {})
    plan = _apply_verdicts(state, [], {0: NS(verdict="merge", existing_memory_id="T", peers=[])})
    got = [(r.conflict_verdict, r.version) for r in state.results]
    assert got == [("merge", 0), ("merge", 0), ("", 1), ("clear", 1)]
    assert plan.merged == ["a", "a"]
    assert plan.reinforce_ids == ["T"] and state.restated_ids == ["T"]
    assert [x["memory_key"] for x in plan.memory_rows] == ["a", "t"]


def test_a_restated_landing_keeps_the_merge_peers_on_the_merging_entry() -> None:
    # a=Berlin merges into T, t is rewritten, a=Berlin again lands. The first a answers
    # a merge naming T, as memory.set does; the landing names no peer (T's old text).
    from server.memory.app.bulk_rows import _bulk_skip_gate
    from server.memory.app.bulk_set import _apply_verdicts

    peer_t = ConflictPeer(
        memory_id="T", memory_key="t", similarity=0.95, pinned=False, canonical=""
    )
    merge = ConflictResult(verdict="merge", peers=[peer_t], existing_memory_id="T")
    state = _stored(t=("T", "berlin"))
    _entry(state, "a", "A1", "berlin")
    _entry(state, "t", "T", "bike")
    assert _bulk_skip_gate(NS(memory_key="a"), "berlin", state, {})
    _apply_verdicts(state, [], {0: merge})
    first, last = state.results[0], state.results[2]
    assert first.conflict_verdict == "merge" and [p.memory_id for p in first.peers] == ["T"]
    assert last.conflict_verdict == "clear" and last.peers == [] and last.next == ""
    assert last.memory_id == "A1" and last.version == 1


def test_a_repeat_with_other_labels_is_not_a_restatement() -> None:
    # Same key and bytes as an earlier entry, but a new sensitivity, pin or entity list:
    # memory.set would write it, so the bulk builds its own row for it.
    from server.memory.app.bulk_rows import _bulk_skip_gate

    for changed in ({"sensitivity": "high"}, {"pinned": True}, {"entities": ["Alice"]}):
        state = _stored()
        _entry(state, "a", "A1", "berlin")
        assert not _bulk_skip_gate(NS(memory_key="a", **changed), "berlin", state, {})
        assert [s.repeats for s in state.steps] == [None]


def test_a_stored_value_repeated_with_a_new_label_is_written() -> None:
    # The stored row holds these bytes. A new sensitivity or a new pin writes a version,
    # as memory.set does; the same labels stay unchanged. A pinned or locked row does not
    # refuse the same bytes.
    from server.memory.app.bulk_rows import _bulk_skip_gate

    for entry, skips in (
        (NS(memory_key="k", value="v"), True),
        (NS(memory_key="k", value="v", sensitivity="high"), False),
        (NS(memory_key="k", value="v", pinned=True), False),
    ):
        state = _stored(k=("K1", "s1"))
        assert _bulk_skip_gate(entry, "s1", state, {}) is skips
    state = _stored(k=("K1", "s1"))
    state.existing_pinned = {"k": True}
    assert not _bulk_skip_gate(NS(memory_key="k", value="v", sensitivity="high"), "s1", state, {})
    assert state.skipped_pinned == []


def test_a_repeat_that_relabels_a_landed_row_lands_and_keeps_its_pin() -> None:
    # k=v lands pinned; k=v again as 'high' lands too, still pinned (the same bytes keep
    # the pin); a third with the same labels but another source is unchanged.
    state = _stored()
    _entry(state, "k", "id1", "s1", pinned=True)
    _entry(state, "k", "id1", "s1", sensitivity="high")
    _entry(state, "k", "id1", "s1", sensitivity="high", source_trace="again")
    assert _settle(state, {}) == {2}
    assert state.memory_rows[1]["pinned"] == 1


def test_a_restatement_of_a_landed_entry_counts_once_as_unchanged() -> None:
    from server.memory.app.bulk_rows import _bulk_skip_gate
    from server.memory.app.bulk_set import _apply_verdicts

    state = _stored()
    _entry(state, "k", "id1", "s1")
    assert _bulk_skip_gate(NS(memory_key="k"), "s1", state, {})
    _apply_verdicts(state, [], {})
    assert state.skipped_unchanged == 1 and state.restated_ids == ["id1"]


def test_a_peer_rewritten_by_a_stale_merge_that_lands_is_not_marked() -> None:
    # Stored: k1 (X), k2 (Y, "b"), k3 (Z). Entry 0 rewrites X. Entry 1 sets k2 to "b2"
    # and merges into X, whose bytes entry 0 replaced, so it lands and rewrites Y.
    # Entry 2 contradicts or refines Y's old "b": marking Y would hit "b2".
    from server.memory.app.bulk_set import _apply_verdicts

    peer_y = ConflictPeer(
        memory_id="Y", memory_key="k2", similarity=0.9, pinned=False, canonical=""
    )
    for verdict in ("conflict", "refines"):
        state = _stored(k1=("X", "a"), k2=("Y", "b"), k3=("Z", "c"))
        _entry(state, "k1", "X", "a2")
        _entry(state, "k2", "Y", "b2")
        _entry(state, "k3", "Z", "c2")
        verdicts = {
            1: NS(verdict="merge", existing_memory_id="X", peers=[]),
            2: ConflictResult(verdict=verdict, peers=[peer_y], refined_peers=[peer_y]),
        }
        plan = _apply_verdicts(state, [], verdicts)
        assert [x["memory_key"] for x in plan.memory_rows] == ["k1", "k2", "k3"]
        assert plan.conflict_marks == [] and plan.refines == []
        assert state.results[2].conflict_verdict == "clear"


def test_a_refused_entry_names_its_pinned_blocker() -> None:
    # memory.set's 409 names the pinned row that refused it; the bulk result does too.
    from server.memory.app.bulk_rows import _bulk_skip_gate

    pinned = ConflictPeer(memory_id="P", memory_key="p", similarity=0.9, pinned=True, canonical="")
    v = ConflictResult(verdict="rejected", peers=[pinned], pinned_blocker_id="P")
    m, e, ix, r = _rows(1)
    for res in r:
        res.pinned_blocker_id = ""
    apply_bulk_verdicts(m, e, ix, r, {0: v}, stale_rows=(), landed_rows=_landing(m, 0))
    assert r[0].conflict_verdict == "rejected" and r[0].pinned_blocker_id == "P"
    state = _stored(k=("K1", "s0"))
    state.existing_pinned = {"k": True}
    assert _bulk_skip_gate(NS(memory_key="k"), "s1", state, {})
    assert state.results[0].pinned_blocker_id == "K1"


async def test_the_bulk_audit_counts_a_conflict_only_once_its_mark_landed(monkeypatch) -> None:
    # The peer was retired before the mark: the result says clear, so must the audit.
    import asyncio

    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
    from server.memory.app import bulk_set, conflicts
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_verdicts import BulkVerdictPlan

    async def nothing(*a, **k) -> None:
        return None

    for name in ("write_entity_vectors", "write_entity_edges", "write_entity_triples"):
        monkeypatch.setattr(bulk_set, name, nothing)
    monkeypatch.setattr(bulk_set, "write_derived_facts", nothing)

    async def mark(**k) -> str:
        await asyncio.sleep(0.01)  # a round trip: the other legs run meanwhile
        return "clear"

    audited: list[list[str]] = []

    async def audit(input, rows, plan) -> None:
        audited.append(list(plan.conflicted))

    monkeypatch.setattr(conflicts, "mark_peers_conflicted", mark)
    monkeypatch.setattr(bulk_set, "_audit_bulk_write", audit)
    write = MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=[{"memory_key": "k", "value": "v"}]
    )
    state = _BulkWriteState(existing_ids={"k": "id1"})
    state.results = [
        _BulkSetResult(memory_key="k", version=0, memory_id="id1", conflict_verdict="conflict")
    ]
    state.key_of = {"id1": "k"}
    plan = BulkVerdictPlan()
    plan.conflicted, plan.conflict_marks = ["k"], [("id1", ["p"])]
    await bulk_set._bulk_run_projections(None, write, state, plan, None, {}, {})
    assert state.results[0].conflict_verdict == "clear"
    assert audited == [[]]
