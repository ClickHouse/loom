"""Bulk writes get memory.set's four conflict verdicts. The
fold from ConflictResults onto the rows about to be inserted is pure."""

from __future__ import annotations

from types import SimpleNamespace as NS

from server.memory.app.bulk_verdicts import apply_bulk_verdicts


def _rows(n: int):
    memory_rows = [
        {"memory_key": f"k{i}", "memory_id": f"id{i}", "status": "active"} for i in range(n)
    ]
    embedding_rows = [{"memory_key": f"k{i}", "status": "active"} for i in range(n)]
    emb_idx_of = list(range(n))
    results = [NS(memory_key=f"k{i}", version=100 + i, memory_id=f"id{i}") for i in range(n)]
    return memory_rows, embedding_rows, emb_idx_of, results


def test_no_verdicts_is_a_pass_through() -> None:
    m, e, ix, r = _rows(3)
    plan = apply_bulk_verdicts(m, e, ix, r, {})
    assert plan.memory_rows == m and plan.embedding_rows == e
    assert plan.unchecked == 0 and not plan.conflict_marks


def test_merge_and_rejected_drop_the_row_and_zero_its_result() -> None:
    m, e, ix, r = _rows(4)
    verdicts = {
        0: NS(verdict="merge", existing_memory_id="old0", peers=[]),
        2: NS(verdict="rejected", pinned_blocker_id="pin", peers=[]),
    }
    plan = apply_bulk_verdicts(m, e, ix, r, verdicts)
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
    plan = apply_bulk_verdicts(m, e, ix, r, verdicts)
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
    plan = apply_bulk_verdicts(m, e, ix, r, {2: NS(verdict="rejected", peers=[])})
    assert [x["memory_key"] for x in plan.memory_rows] == ["k0", "k1"]
    assert [x["memory_key"] for x in plan.embedding_rows] == ["k0"]


def test_unchecked_counts_searchable_rows_past_the_cap() -> None:
    m, e, ix, r = _rows(5)
    plan = apply_bulk_verdicts(
        m, e, ix, r, {0: NS(verdict="clear", peers=[]), 1: NS(verdict="clear", peers=[])}
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
        m, e, ix, r, {2: NS(verdict="duplicate", duplicate_of_id="dup", peers=[])}
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
        plan = apply_bulk_verdicts(m, e, ix, r, {0: v})
        assert not plan.conflict_marks and not plan.refines
        assert not plan.conflicted and not plan.refined
        assert r[0].conflict_verdict == "clear"
        plan = apply_bulk_verdicts(*rows(), {1: v})
        assert len(plan.conflict_marks) + len(plan.refines) == 1
        plan = apply_bulk_verdicts(*rows(), {0: v, 1: NS(verdict="merge", peers=[])})
        assert len(plan.conflict_marks) + len(plan.refines) == 1


def test_a_merge_with_a_row_an_earlier_entry_rewrote_lands() -> None:
    # Row 0 rewrites id0; rows 1 and 2 matched id0's stored bytes, which are gone. A
    # target rewritten only after the entry still merges, as memory.set in that order would.
    m, e, ix, r = _rows(3)
    verdicts = {
        1: NS(verdict="merge", existing_memory_id="id0", peers=[]),
        2: NS(verdict="duplicate", duplicate_of_id="id0", peers=[]),
    }
    plan = apply_bulk_verdicts(m, e, ix, r, verdicts)
    assert [x["memory_key"] for x in plan.memory_rows] == ["k0", "k1", "k2"]
    assert plan.merged == [] and plan.duplicates == [] and plan.reinforce_ids == []
    assert r[1].conflict_verdict == "clear" and r[1].version == 101
    late = apply_bulk_verdicts(*_rows(2), {0: NS(verdict="merge", existing_memory_id="id1")})
    assert late.merged == ["k0"]
