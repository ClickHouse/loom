"""What Loom retires on its own leaves one `memory.auto_supersede` audit row per
path: ids, keys and identity (never value text), the reason, and only the rows that
did retire."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import auto_resolution as ar


@pytest.fixture
def emitted(monkeypatch) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    async def _emit(event_type, **kw):
        out.append({"event_type": event_type, **kw})
        return "h"

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    return out


def _retired(mid: str, *, by: str = "new", key: str = "user_city", **extra: Any) -> dict:
    return {
        "memory_id": mid,
        "memory_key": key,
        "namespace": "ns",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "superseded_by": by,
        "value": f"secret text of {mid}",
        **extra,
    }


def _ids(event: dict) -> list[str]:
    return [e["memory_id"] for e in event["subject"]["retired"]]


async def test_the_event_names_rows_and_reason_never_value_text(emitted) -> None:
    await ar.record_auto_supersessions(
        org="o",
        namespace="ns",
        retired=[_retired("m1", label="contradiction", superseded_by_key="user_city")],
        reason=ar.NEWEST_WINS_CONTRADICTION,
    )
    (ev,) = emitted
    assert (ev["event_type"], ev["org"], ev["namespace"]) == ("memory.auto_supersede", "o", "ns")
    assert ev["actor"] == "process:loom"
    assert ev["subject"] == {
        "reason": "newest_wins_contradiction",
        "count": 1,
        "retired": [
            {
                "memory_id": "m1",
                "memory_key": "user_city",
                "agent": "a",
                "user_id": "u",
                "session_id": "s",
                "superseded_by": "new",
                "superseded_by_key": "user_city",
                "label": "contradiction",
            }
        ],
    }
    assert "secret" not in repr(ev)


async def test_more_than_the_cap_splits_into_several_rows_listing_every_one(emitted) -> None:
    rows = [_retired(f"m{i}") for i in range(450)]
    await ar.record_auto_supersessions(org="o", namespace="ns", retired=rows, reason="refines")
    assert [ev["subject"]["count"] for ev in emitted] == [200, 200, 50]
    assert [len(ev["subject"]["retired"]) for ev in emitted] == [200, 200, 50]
    assert [m for ev in emitted for m in _ids(ev)] == [f"m{i}" for i in range(450)]
    assert {ev["subject"]["reason"] for ev in emitted} == {"refines"}


async def test_the_record_carries_the_request_id(emitted) -> None:
    from server.core.logging_config import request_id_var

    token = request_id_var.set("req-42")
    try:
        await ar.record_auto_supersessions(
            org="o", namespace="ns", retired=[_retired("m1")], reason="refines"
        )
    finally:
        request_id_var.reset(token)
    assert emitted[0]["request_id"] == "req-42"


async def test_nothing_retired_writes_nothing(emitted) -> None:
    await ar.record_auto_supersessions(org="o", namespace="ns", retired=[], reason="refines")
    ar.schedule_auto_supersessions(org="o", namespace="ns", retired=[], reason="refines")
    await _drain()
    assert emitted == []


async def test_a_failing_audit_write_never_raises_and_says_so(monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("server.observability.audit.emit", boom)
    assert (
        await ar.record_auto_supersessions(
            org="o", namespace="ns", retired=[_retired("m1")], reason="refines"
        )
        is False
    )
    ar.schedule_auto_supersessions(org="o", namespace="ns", retired=[_retired("m1")], reason="x")
    await _drain()


async def test_a_scheduled_record_lands_beside_the_caller(emitted) -> None:
    ar.schedule_auto_supersessions(
        org="o", namespace="ns", retired=[_retired("m1")], reason="refines"
    )
    assert emitted == []  # not awaited by the caller
    await _drain()
    assert _ids(emitted[0]) == ["m1"]


def test_the_tracker_records_only_rows_it_passed_that_were_written() -> None:
    t = ar.RetirementTracker(lambda row: row.memory_id == "locked")
    assert t.skip(SimpleNamespace(memory_id="ok", status="active", pinned=False)) is False
    assert t.skip(SimpleNamespace(memory_id="dup", status="duplicate", pinned=False)) is False
    assert t.skip(SimpleNamespace(memory_id="locked", status="active", pinned=False)) is True
    # A pin is a write-time invariant: nothing automatic retires it.
    assert t.skip(SimpleNamespace(memory_id="pin", status="active", pinned=True)) is True
    # Retired already (or tombstoned, proposed): not re-pointed, not recorded.
    for status in ("superseded", "tombstoned", "proposed"):
        assert t.skip(SimpleNamespace(memory_id="x", status=status, pinned=False)) is True
    t.written([{"memory_id": "ok", "version": 7}])
    victims = [{"memory_id": m} for m in ("ok", "dup", "locked", "never_read")]
    # `dup` passed the skip but its retirement never took effect.
    assert t.retired(victims) == [{"memory_id": "ok"}]


async def _drain() -> None:
    from server.memory.app import after_write

    await after_write.drain()


# --- the call sites -------------------------------------------------------------


def _fake_supersede(monkeypatch, stored: dict[str, str] | set[str], *, fail: str = "") -> list[str]:
    """`supersede_duplicates` as the store runs it: `skip` is asked only about a row
    it could read (`stored`: id -> status), `on_written` hears the rows it inserted,
    and `fail="insert"` / `"mirror"` raises before / after that insert. Returns the
    ids it wrote."""
    from server.memory import store

    statuses = stored if isinstance(stored, dict) else dict.fromkeys(stored, "active")
    wrote: list[str] = []

    async def supersede(
        org,
        victims,
        *,
        ch=None,
        skip=None,
        strong=False,
        on_written=None,
        on_unconfirmed=None,
        judged=False,
    ):
        # A lagging replica's row would be rebuilt over a revision at a higher version.
        assert strong, "the retirement reads read-your-writes"
        rows = [
            SimpleNamespace(memory_id=v["memory_id"], status=statuses[v["memory_id"]], pinned=False)
            for v in victims
            if v["memory_id"] in statuses
        ]
        rows = [r for r in rows if not (skip and skip(r))]
        if fail == "insert":
            raise RuntimeError("insert failed")
        wrote.extend(r.memory_id for r in rows)
        if on_written is not None:
            on_written([{"memory_id": r.memory_id, "version": 9} for r in rows])
        if fail == "mirror":
            raise RuntimeError("mirror failed")
        return len(rows)

    monkeypatch.setattr(store, "supersede_duplicates", supersede)
    return wrote


def _lock(monkeypatch, *locked: str) -> None:
    from server.memory.domain import lock

    monkeypatch.setattr(lock, "refuses_retirement", lambda row: row.memory_id in locked)


class _CH:
    def __init__(self, *, all_: list[list[dict]] | None = None, one: list[dict] | None = None):
        self.all, self.one = list(all_ or []), list(one or [])

    async def query_all(self, sql, params=None):
        return self.all.pop(0) if self.all else []

    async def query_one(self, sql, params=None):
        return self.one.pop(0) if self.one else None


def _live(mid: str, key: str = "user_city") -> dict:
    return {
        "memory_id": mid,
        "namespace": "ns",
        "agent": "a",
        "user_id": "u",
        "session_id": f"s-{mid}",
        "memory_key": key,
        "value": "v",
        "pinned": 0,
    }


async def test_newest_wins_same_key_records_the_retired_not_the_locked_or_retired(
    monkeypatch, emitted
) -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    wrote = _fake_supersede(
        monkeypatch, {"o1": "active", "locked": "active", "retired": "superseded"}
    )
    _lock(monkeypatch, "locked")
    new = [_live("n1")]
    live = [new[0], _live("o1"), _live("locked"), _live("retired"), _live("gone")]
    n = await supersede_same_key_rows(
        org="o", namespace="ns", new_rows=new, ch=_CH(all_=[live]), policy="newest_wins"
    )
    assert n == 1 and wrote == ["o1"]  # the retired row is not re-pointed
    assert emitted == []  # beside the response, not awaited by the write
    await _drain()
    (ev,) = emitted
    assert ev["subject"]["reason"] == "same_key_newest_wins" and _ids(ev) == ["o1"]
    assert ev["subject"]["retired"][0]["superseded_by"] == "n1"
    assert ev["subject"]["retired"][0]["superseded_by_key"] == "user_city"


async def test_newest_wins_with_nothing_retired_records_nothing(monkeypatch, emitted) -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    _fake_supersede(monkeypatch, {"locked"})
    _lock(monkeypatch, "locked")
    new = [_live("n1")]
    await supersede_same_key_rows(
        org="o",
        namespace="ns",
        new_rows=new,
        ch=_CH(all_=[[new[0], _live("locked")]]),
        policy="newest_wins",
    )
    await _drain()
    assert emitted == []


@pytest.mark.parametrize(("fail", "recorded"), [("mirror", ["o1"]), ("insert", [])])
async def test_newest_wins_records_what_reached_the_table_when_the_write_fails(
    monkeypatch, emitted, fail, recorded
) -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    _fake_supersede(monkeypatch, {"o1"}, fail=fail)
    _lock(monkeypatch)
    new = [_live("n1")]
    await supersede_same_key_rows(
        org="o",
        namespace="ns",
        new_rows=new,
        ch=_CH(all_=[[new[0], _live("o1")]]),
        policy="newest_wins",
    )
    await _drain()
    assert [m for ev in emitted for m in _ids(ev)] == recorded


async def test_the_real_store_reports_what_its_insert_wrote_before_the_mirror_fails(
    monkeypatch,
) -> None:
    from server.memory.store import lifecycle

    rows = {
        "ok": SimpleNamespace(memory_id="ok", status="active", pinned=False),
        "locked": SimpleNamespace(memory_id="locked", status="active", pinned=False),
        "retired": SimpleNamespace(memory_id="retired", status="superseded", pinned=False),
    }
    inserted: list = []

    async def get_by_id(mid, **kw):
        return rows.get(mid)

    async def insert(batch, *, ch=None):
        inserted.extend(batch)

    async def mirror(*a, **k):
        raise RuntimeError("mirror down")

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", _version_row)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", mirror)
    tracker = ar.RetirementTracker(lambda row: row.memory_id == "locked")
    victims = [{"memory_id": m, "superseded_by": "n1"} for m in ("ok", "locked", "retired", "gone")]
    with pytest.raises(RuntimeError, match="mirror down"):
        await lifecycle.supersede_duplicates(
            "o", victims, ch=_Heads(inserted), skip=tracker.skip, on_written=tracker.written
        )
    assert len(inserted) == 1
    assert [v["memory_id"] for v in tracker.retired(victims)] == ["ok"]


def _version_row(row, **overrides):
    """A version row as the store builds it: the identity columns and the overrides."""
    ident = {"namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    return {**ident, "memory_key": row.memory_id, "memory_id": row.memory_id, **overrides}


class _Heads:
    """The head re-read's double: each inserted row is its key's head, unless
    `raced` names a memory a later write at that key overtook."""

    def __init__(self, inserted: list, raced: frozenset[str] = frozenset()) -> None:
        self.inserted, self.raced = inserted, raced

    async def query_all(self, sql, params):
        return [
            {**r, "version": r["version"] + (1 if r["memory_id"] in self.raced else 0)}
            for r in self.inserted
        ]


async def test_a_retirement_a_later_write_overtook_is_not_reported_or_mirrored(monkeypatch) -> None:
    # A no_delete lock landed between the retirement's read and its insert, at a
    # higher version: the head is still active and locked.
    from server.memory.store import lifecycle

    rows = {m: SimpleNamespace(memory_id=m, status="active", pinned=False) for m in ("won", "lost")}
    inserted: list = []
    mirrored: list = []

    async def get_by_id(mid, **kw):
        return rows.get(mid)

    async def insert(batch, *, ch=None):
        inserted.extend(batch)

    async def mirror(batch, *, ch=None, **_):
        mirrored.extend(r["memory_id"] for r in batch)

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", _version_row)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", mirror)
    tracker = ar.RetirementTracker()
    victims = [{"memory_id": m, "superseded_by": "n1"} for m in ("won", "lost")]
    n = await lifecycle.supersede_duplicates(
        "o",
        victims,
        ch=_Heads(inserted, raced=frozenset({"lost"})),
        skip=tracker.skip,
        on_written=tracker.written,
    )
    assert n == 1 and [v["memory_id"] for v in tracker.retired(victims)] == ["won"]
    assert mirrored == ["won"]


async def test_the_real_store_reports_nothing_when_its_insert_fails(monkeypatch) -> None:
    from server.memory.store import lifecycle

    async def get_by_id(mid, **kw):
        return SimpleNamespace(memory_id=mid, status="active", pinned=False)

    async def insert(batch, *, ch=None):
        raise RuntimeError("insert down")

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", lambda row, **o: dict(o))
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    tracker = ar.RetirementTracker()
    victims = [{"memory_id": "ok", "superseded_by": "n1"}]
    with pytest.raises(RuntimeError, match="insert down"):
        await lifecycle.write_versions_by_ids(
            "o",
            ["ok"],
            lambda row: {"status": "superseded"},
            ch=object(),
            skip=tracker.skip,
            on_written=tracker.written,
        )
    # The skip passed the row, but its version never landed.
    assert tracker.retired(victims) == []


class _Input:
    org, namespace, agent = "o", "ns", "a"


async def test_intra_batch_newest_wins_records_the_earlier_row(emitted) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    state = _BulkWriteState()
    state.memory_rows = [
        {**_live("old", "capital_a"), "status": "superseded", "superseded_by": "new"},
        {**_live("new", "capital_b"), "status": "active"},
    ]
    _index(state)

    await bulk_set._record_intra_batch_retirements(_Input(), state, [("old", "new")])
    (ev,) = emitted
    assert ev["subject"]["reason"] == "newest_wins_contradiction" and _ids(ev) == ["old"]
    assert ev["subject"]["retired"][0]["superseded_by_key"] == "capital_b"
    await bulk_set._record_intra_batch_retirements(_Input(), state, [])
    assert len(emitted) == 1


async def test_intra_batch_records_only_the_kept_version_of_a_key_sent_twice(emitted) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    # capital_a twice: two ids, one identity, and FINAL keeps the higher version (a2).
    state = _BulkWriteState()
    state.memory_rows = [
        {**_live("a1", "capital_a"), "version": 1, "status": "superseded"},
        {**_live("b", "capital_b"), "version": 2, "status": "active"},
        {**_live("a2", "capital_a"), "version": 3, "status": "active"},
    ]
    _index(state)
    # a1 lost to b, but a2 (the version FINAL keeps) is live: nothing retired.
    await bulk_set._record_intra_batch_retirements(_Input(), state, [("a1", "b")])
    assert emitted == []
    # a2 lost to b, once though the pair is listed twice.
    state.memory_rows[2]["status"] = "superseded"
    await bulk_set._record_intra_batch_retirements(_Input(), state, [("a2", "b"), ("a2", "b")])
    (ev,) = emitted
    assert _ids(ev) == ["a2"]


async def test_intra_batch_records_nothing_when_an_existing_key_is_revised_twice(emitted) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    # An existing key revised twice reuses its stored id: both versions share "a".
    # Only v1 lost to b; FINAL keeps v3, which is active, so nothing was retired.
    state = _BulkWriteState()
    state.memory_rows = [
        {**_live("a", "capital_a"), "version": 1, "status": "superseded"},
        {**_live("b", "capital_b"), "version": 2, "status": "active"},
        {**_live("a", "capital_a"), "version": 3, "status": "active"},
    ]
    _index(state)
    await bulk_set._record_intra_batch_retirements(_Input(), state, [("a", "b")])
    assert emitted == []


async def test_intra_batch_records_the_row_its_kept_version_retired_behind(emitted) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    # Both versions of "x" are the older row of a pair; FINAL keeps v2, which retired
    # behind "z". The record names z, as the Undo's precondition will.
    state = _BulkWriteState()
    state.memory_rows = [
        {**_live("x", "capital_a"), "version": 1, "status": "superseded", "superseded_by": "y"},
        {**_live("x", "capital_a"), "version": 2, "status": "superseded", "superseded_by": "z"},
        {**_live("z", "capital_c"), "version": 3, "status": "active"},
        {**_live("y", "capital_b"), "version": 4, "status": "active"},
    ]
    _index(state)
    await bulk_set._record_intra_batch_retirements(_Input(), state, [("x", "z"), ("x", "y")])
    (ev,) = emitted
    (entry,) = ev["subject"]["retired"]
    assert (entry["memory_id"], entry["superseded_by"]) == ("x", "z")


async def test_a_bulk_writes_one_record_for_all_its_refines_verdicts(monkeypatch, emitted) -> None:
    from server.memory.app import bulk_set, conflicts
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_verdicts import BulkVerdictPlan

    async def refines(**kw):
        assert kw["record_into"] is not None
        kw["record_into"].setdefault("newest_wins_contradiction", []).append(
            {**_live(f"old-{kw['new_memory_id']}"), "superseded_by": kw["new_memory_id"]}
        )
        return 1

    monkeypatch.setattr(conflicts, "apply_refines", refines)
    plan = BulkVerdictPlan()
    plan.refines = [(f"n{i}", f"k{i}", []) for i in range(3)]
    await bulk_set._bulk_settle_verdicts(None, _Input(), _BulkWriteState(), plan)
    (ev,) = emitted
    assert _ids(ev) == ["old-n0", "old-n1", "old-n2"]


async def test_a_bulk_mark_with_no_live_peer_left_answers_clear(monkeypatch) -> None:
    # Another writer retired the peer after the verdict: the row stays active, unflagged.
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.api.models_common import MemorySetPeer
    from server.memory.app import bulk_set, conflicts
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_verdicts import BulkVerdictPlan

    async def mark(**kw):
        return "clear"

    monkeypatch.setattr(conflicts, "mark_peers_conflicted", mark)
    state = _BulkWriteState()
    state.memory_rows = [{"memory_id": "n1", "memory_key": "user_city"}]
    _index(state)
    peer = MemorySetPeer(memory_id="p", memory_key="user_city")
    state.results = [
        _BulkSetResult(memory_key="user_city", version=1, conflict_verdict="conflict", peers=[peer])
    ]
    plan = BulkVerdictPlan()
    plan.conflicted, plan.conflict_marks = ["user_city"], [("n1", ["p"])]
    await bulk_set._bulk_settle_verdicts(None, _Input(), state, plan)
    assert state.results[0].conflict_verdict == "clear" and plan.conflicted == []


async def _restatement_reinforces(monkeypatch, state: Any, verdict: Any) -> list[str]:
    """Entry 0 writes `state`'s key with value sha "s2" (its verdict `verdict`); entry 1
    says s2 again. Returns the ids the write reinforced."""
    from types import SimpleNamespace as NS

    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _bulk_skip_gate, _keep_row, _row_id
    from server.memory.store import usage

    reinforced: list[str] = []

    async def record(**kw):
        reinforced.append(kw["memory_id"])

    monkeypatch.setattr(usage, "record_usage_event", record)
    row = {"memory_key": "k", "content_sha256": "s2", "pinned": False}
    row["memory_id"] = _row_id(state, "k")
    state.row_results.append(0)
    state.results.append(_BulkSetResult(memory_key="k", version=1, memory_id=row["memory_id"]))
    _keep_row(state, row, NS(memory_key="k"))
    assert _bulk_skip_gate(NS(memory_key="k"), "s2", state, {})
    plan = bulk_set._apply_verdicts(state, [], {0: verdict})
    await bulk_set._bulk_settle_verdicts(None, _Input(), state, plan)
    return reinforced


async def test_a_restatement_of_a_merged_new_key_reinforces_the_merge_target(
    monkeypatch,
) -> None:
    # Entry 0 creates key k and merges into the stored row "old", so no row for k lands;
    # entry 1 says k's value again. Its evidence goes to "old", never to k's minted id.
    from types import SimpleNamespace as NS

    from server.memory.app.bulk_rows import _BulkWriteState

    merge = NS(verdict="merge", existing_memory_id="old", peers=[])
    assert await _restatement_reinforces(monkeypatch, _BulkWriteState(), merge) == ["old", "old"]


async def test_a_restatement_of_a_revision_reinforces_the_row_the_revision_reached(
    monkeypatch,
) -> None:
    # Stored k is "Berlin" (row e1); entry 0 revises it to "Paris", entry 1 says "Paris"
    # again. e1 never held "Paris" if entry 0 merged elsewhere or was rejected.
    from types import SimpleNamespace as NS

    from server.memory.app.bulk_rows import _BulkWriteState

    def stored() -> _BulkWriteState:
        state = _BulkWriteState()
        state.existing_ids, state.existing_sha = {"k": "e1"}, {"k": "s1"}
        return state

    merge = NS(verdict="merge", existing_memory_id="old", peers=[])
    rejected = NS(verdict="rejected", peers=[])
    clear = NS(verdict="clear", peers=[])
    assert await _restatement_reinforces(monkeypatch, stored(), merge) == ["old", "old"]
    assert await _restatement_reinforces(monkeypatch, stored(), rejected) == []
    assert await _restatement_reinforces(monkeypatch, stored(), clear) == ["e1"]


async def test_a_restatement_of_a_stored_value_reinforces_the_stored_row(monkeypatch) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_verdicts import BulkVerdictPlan
    from server.memory.store import usage

    reinforced: list[str] = []

    async def record(**kw):
        reinforced.append(kw["memory_id"])

    monkeypatch.setattr(usage, "record_usage_event", record)
    state = _BulkWriteState()
    state.existing_ids = {"live": "e1"}
    state.restated_ids = ["e1"]
    await bulk_set._bulk_settle_verdicts(None, _Input(), state, BulkVerdictPlan())
    assert reinforced == ["e1"]


def _peer(mid: str, key: str = "user_city") -> Any:
    from server.memory.domain.conflicts import ConflictPeer

    return ConflictPeer(
        memory_id=mid,
        memory_key=key,
        similarity=0.9,
        pinned=False,
        canonical="c",
        agent="a",
        user_id="u",
        session_id="s",
        value="lives in a city",
    )


async def _refines(monkeypatch, **kw: Any) -> None:
    from server.memory import store
    from server.memory.app.conflicts import apply_refines

    async def no_row(*a, **k):
        return None

    monkeypatch.setattr(store, "get_by_id", no_row)
    await apply_refines(
        org="o",
        namespace="ns",
        new_memory_id="n1",
        # The peers share the key, so no refines edge is written.
        new_memory_key="user_city",
        refined_peers=[_peer("o1"), _peer("locked"), _peer("retired")],
        ch=object(),
        **kw,
    )


_PEERS = {"o1": "active", "locked": "active", "retired": "superseded"}


async def test_refines_records_the_victims_it_superseded(monkeypatch, emitted) -> None:
    wrote = _fake_supersede(monkeypatch, _PEERS)
    _lock(monkeypatch, "locked")
    await _refines(monkeypatch)
    assert wrote == ["o1"]  # an already-retired peer is not re-pointed
    await _drain()
    (ev,) = emitted
    assert ev["subject"]["reason"] == "newest_wins_contradiction" and _ids(ev) == ["o1"]
    assert ev["subject"]["retired"][0]["superseded_by_key"] == "user_city"


async def test_refines_into_a_callers_record_emits_nothing_itself(monkeypatch, emitted) -> None:
    _fake_supersede(monkeypatch, _PEERS)
    _lock(monkeypatch, "locked")
    into: dict[str, list[dict]] = {}
    await _refines(monkeypatch, record_into=into)
    await _drain()
    assert emitted == []
    assert [e["memory_id"] for e in into["newest_wins_contradiction"]] == ["o1"]


@pytest.mark.parametrize(("fail", "recorded"), [("mirror", ["o1"]), ("insert", [])])
async def test_a_failed_refines_write_records_what_reached_the_table(
    monkeypatch, emitted, fail, recorded
) -> None:
    _fake_supersede(monkeypatch, _PEERS, fail=fail)
    _lock(monkeypatch, "locked")
    await _refines(monkeypatch)
    await _drain()
    assert [m for ev in emitted for m in _ids(ev)] == recorded


async def _value_updates(
    monkeypatch, older: dict[str, dict], unconfirmed: frozenset[str] = frozenset()
) -> tuple[Any, list[str]]:
    """`link_value_updates` over new rows n<i> -> stored older rows (id -> row fields,
    or absent: vanished), with `write_versions_by_ids` as the store runs it; the re-read
    cannot confirm the `unconfirmed` ones."""
    from server.memory import store
    from server.memory.app import value_updates as vu
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_write_candidate_batch", 0)
    olds = list(older) + ["gone"]

    async def peers(*a, **k):
        return []

    def links(new_id, new_key, *a, **k):
        i = int(new_id[1:])
        return [vu.UpdateLink(olds[i], new_id, f"old_{new_key}", new_key, 0.9)]

    wrote: list[str] = []

    async def write(
        org, ids, update, *, ch=None, skip=None, strong=False, on_written=None, on_unconfirmed=None
    ):
        assert strong, "the retirement reads read-your-writes"
        found = [
            SimpleNamespace(
                **{
                    **_live(mid, f"old_{mid}"),
                    "superseded_by": "",
                    "status": "active",
                    **older[mid],
                }
            )
            for mid in ids
            if mid in older
        ]
        found = [r for r in found if not (skip and skip(r))]
        for row in found:
            update(row)
        wrote.extend(r.memory_id for r in found)
        written = [{**vars(r), "version": 9} for r in found]
        if on_written is not None:
            on_written([r for r in written if r["memory_id"] not in unconfirmed])
        if on_unconfirmed is not None and unconfirmed:
            await on_unconfirmed([r for r in written if r["memory_id"] in unconfirmed])

    monkeypatch.setattr(vu, "_peers", peers)
    monkeypatch.setattr(vu, "decide_links", links)
    monkeypatch.setattr(store, "write_versions_by_ids", write)
    rows = [
        {"memory_id": f"n{i}", "memory_key": f"k{i}", "value": "x", "valid_at": datetime.now(UTC)}
        for i in range(len(olds))
    ]
    report = await vu.link_value_updates(
        org="o",
        namespace="ns",
        rows=rows,
        embeddings_by_key={r["memory_key"]: [0.1] for r in rows},
        ch=object(),
    )
    return report, wrote


async def test_value_update_records_the_older_rows_it_re_emitted(monkeypatch, emitted) -> None:
    report, wrote = await _value_updates(monkeypatch, {"o1": {}})
    # The vanished older row retired nothing, so it is neither linked nor recorded.
    assert [ln.older_id for ln in report.links] == ["o1"] and wrote == ["o1"]
    (ev,) = emitted
    assert ev["subject"]["reason"] == "value_update" and _ids(ev) == ["o1"]
    entry = ev["subject"]["retired"][0]
    assert (entry["superseded_by"], entry["superseded_by_key"]) == ("n0", "k0")
    assert entry["memory_key"] == "old_o1" and "value" not in entry


async def test_value_update_leaves_locked_pinned_and_retired_rows_live(
    monkeypatch, emitted
) -> None:
    _lock(monkeypatch, "locked")
    report, wrote = await _value_updates(
        monkeypatch,
        {
            "o1": {},
            "locked": {},
            "pinned": {"pinned": True},
            "retired": {"status": "superseded"},
        },
    )
    assert wrote == ["o1"]
    assert [ln.older_id for ln in report.links] == ["o1"]
    (ev,) = emitted
    assert _ids(ev) == ["o1"]


async def test_newest_wins_triple_repair_records_the_retired_carrier(monkeypatch, emitted) -> None:
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    async def policy(*a, **k):
        return "newest_wins"

    monkeypatch.setattr(proj._conflicts, "namespace_conflict_policy", policy)
    wrote = _fake_supersede(
        monkeypatch, {"m-old": "active", "m-locked": "active", "m-gone": "superseded"}
    )
    _lock(monkeypatch, "m-locked")
    links = [
        {"subject": "g", "relation": "r", "object": "old", "memory_key": k}
        for k in ("k_old", "k_locked", "k_gone")
    ] + [{"subject": "g", "relation": "r", "object": "new", "memory_key": "k_new"}]
    mems = [
        _live("m-old", "k_old"),
        _live("m-locked", "k_locked"),
        _live("m-gone", "k_gone"),
        _live("m-new", "k_new"),
    ]
    repair = IntervalRepair(
        subject="g",
        relation="r",
        old_object="old",
        new_object="new",
        valid_to=datetime(2026, 9, 12),
        invalid_at=datetime(2026, 9, 12),
    )
    tm = [{"subject": "g", "relation": "r", "object": "new", "memory_key": "k_new"}]
    n = await proj._retire_memories_behind_repairs(
        SimpleNamespace(org="o", namespace="ns"), [repair], tm, ch=_CH(all_=[links, mems])
    )
    assert n == 1 and wrote == ["m-old"]
    (ev,) = emitted
    assert ev["subject"]["reason"] == "newest_wins_contradiction" and _ids(ev) == ["m-old"]
    assert ev["subject"]["retired"][0]["superseded_by_key"] == "k_new"


async def test_an_unconfirmed_retirement_is_left_for_the_reconcile(monkeypatch) -> None:
    # It may not have landed (a lock may have won), so recall is left as it is and no
    # result counts it; the record marks it, and the reconcile mirrors the head later.
    from server.memory.store import lifecycle

    rows = {m: SimpleNamespace(memory_id=m, status="active", pinned=False) for m in ("a", "b")}
    inserted: list = []
    mirrored: list = []

    async def get_by_id(mid, **kw):
        return rows.get(mid)

    async def insert(batch, *, ch=None):
        inserted.extend(batch)

    async def mirror(batch, *, ch=None):
        mirrored.extend(r["memory_id"] for r in batch)

    class Failing:
        reads = 0

        async def query_all(self, sql, params):
            Failing.reads += 1
            raise RuntimeError("replica down")

    class Lagging:
        async def query_all(self, sql, params):
            return []  # the replica has not seen the insert yet

    class Older:
        async def query_all(self, sql, params):
            # The key is there, at a version below the one just written.
            cols = ("namespace", "agent", "user_id", "session_id", "memory_key")
            return [{**dict(zip(cols, k, strict=True)), "version": 1} for k in params["keys"]]

    queued: list = []

    async def queue(batch):
        queued.extend(r["memory_id"] for r in batch)

    monkeypatch.setattr(ar, "queue_status_mirror", queue)
    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", _version_row)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", mirror)
    victims = [{"memory_id": m, "superseded_by": "n1"} for m in ("a", "b")]
    for ch in (Failing(), Lagging(), Older()):
        mirrored.clear()
        tracker = ar.RetirementTracker()
        n = await lifecycle.supersede_duplicates(
            "o",
            victims,
            ch=ch,
            skip=tracker.skip,
            on_written=tracker.written,
            on_unconfirmed=tracker.unconfirmed,
        )
        assert n == 0 and mirrored == [] and queued[-2:] == ["a", "b"]
        # Recorded and followed as unconfirmed; no result counts them.
        assert [v.get("unconfirmed") for v in tracker.retired(victims)] == [True, True]
    assert Failing.reads == 2, "the head read is tried twice"


async def test_unconfirmed_rows_are_queued_one_failure_per_namespace(monkeypatch) -> None:
    from server.memory.app import projection_failures as pf

    queued: list[dict] = []

    async def record(**kw):
        queued.append(kw)

    monkeypatch.setattr(pf, "record_projection_failure", record)
    rows = [
        {"org": "o", "namespace": "a", "memory_id": "m2", "memory_key": "k"},
        {"org": "o", "namespace": "a", "memory_id": "m1", "memory_key": "k"},
        {"org": "o", "namespace": "b", "memory_id": "m3", "memory_key": "j"},
    ]
    await pf.queue_status_mirror(rows)
    assert [(q["namespace"], q["projection"], q["payload"]) for q in queued] == [
        ("a", pf.STATUS_MIRROR, {"memory_ids": ["m1", "m2"]}),
        ("b", pf.STATUS_MIRROR, {"memory_ids": ["m3"]}),
    ]


async def test_the_reconcile_mirrors_each_rows_head(monkeypatch) -> None:
    from types import SimpleNamespace

    import server.memory.store as memory_repo
    from server.memory.app import projection_failures

    heads = {
        "kept": SimpleNamespace(model_dump=lambda: {"memory_id": "kept", "status": "active"}),
        "deleted": SimpleNamespace(
            model_dump=lambda: {"memory_id": "deleted", "tombstoned_at": "2026-10-14"}
        ),
    }
    mirrored: list = []

    async def get_by_id(mid, **kw):
        assert kw["strong"] and kw["include_tombstoned"]
        return heads.get(mid)

    async def mirror(rows, *, ch=None, strict=False):
        assert strict, "a failed mirror must fail the repair"
        mirrored.extend(rows)

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(memory_repo, "mirror_status_to_embeddings", mirror)
    failure = SimpleNamespace(
        failure_id="f1",
        org="o",
        payload={"memory_ids": ["kept", "deleted", "erased"]},
        projection=projection_failures.STATUS_MIRROR,
    )
    resolved: list = []

    async def mark(**kw):
        resolved.extend(kw["failure_ids"])

    monkeypatch.setattr(projection_failures, "mark_projection_failures_resolved", mark)
    out = await projection_failures.retry_projection_failure(failure, ch=object())
    # Live: mirrored as live. Deleted since: its tombstone is mirrored. Erased: the
    # erasure took its embeddings, nothing to mirror.
    assert mirrored == [
        {"memory_id": "kept", "status": "active"},
        {"memory_id": "deleted", "tombstoned_at": "2026-10-14"},
    ]
    assert out.resolved and resolved == ["f1"]


async def test_a_failed_reconcile_mirror_leaves_the_failure_open(monkeypatch) -> None:
    from types import SimpleNamespace

    import server.memory.store as memory_repo
    from server.memory.app import projection_failures

    async def get_by_id(mid, **kw):
        return SimpleNamespace(model_dump=lambda: {"memory_id": mid})

    async def mirror(rows, *, ch=None, strict=False):
        raise TimeoutError("embeddings insert")

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(memory_repo, "mirror_status_to_embeddings", mirror)
    failure = SimpleNamespace(
        failure_id="f1", org="o", payload={"memory_ids": ["m"]}, projection="memory_status_mirror"
    )
    out = await projection_failures.retry_projection_failure(failure, ch=object())
    assert out.retried and not out.resolved and "embeddings insert" in out.error


async def test_an_unconfirmed_value_update_is_followed_but_not_counted(
    monkeypatch, emitted
) -> None:
    from server.memory.app import auto_resolution as ar

    queued: list = []

    async def queue(rows):
        queued.extend(r["memory_id"] for r in rows)

    monkeypatch.setattr(ar, "queue_status_mirror", queue)
    report, _ = await _value_updates(monkeypatch, {"o1": {}}, unconfirmed=frozenset({"o1"}))
    assert report.links == [] and [ln.older_id for ln in report.follow_links] == ["o1"]
    assert queued == ["o1"]
    (ev,) = emitted
    assert ev["subject"]["retired"][0]["unconfirmed"] == "true"


async def _marks(
    monkeypatch,
    peers: dict[str, str | None],
    at_write: dict[str, str] | None = None,
    source: str = "active",
) -> list[tuple[str, dict]]:
    """`mark_peers_conflicted` of n1 against `peers` (id -> status, None for a row no
    longer stored), with a store that applies `skip` as the real one does and sees
    the `at_write` statuses by the time it writes; the (id, overrides) it wrote."""
    from types import SimpleNamespace

    import server.memory.store as memory_repo
    from server.memory.app.conflicts import mark_peers_conflicted

    rows = {"n1": source, **{p: st for p, st in peers.items() if st is not None}}
    wrote: list[tuple[str, dict]] = []

    async def get_by_id(mid, **kw):
        assert kw["strong"]
        return SimpleNamespace(status=rows[mid], conflict_with=[]) if mid in rows else None

    statuses = {**rows, **(at_write or {})}

    async def write(org, ids, update, *, skip=None, strong=False, **kw):
        assert strong, "both writes read their row read-your-writes"
        n = 0
        for mid in ids:
            row = SimpleNamespace(status=statuses[mid], conflict_with=[])
            if skip is None or not skip(row):
                wrote.append((mid, update(row)))
                n += 1
        return n

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(memory_repo, "write_versions_by_ids", write)
    await mark_peers_conflicted(org="o", new_memory_id="n1", peer_ids=list(peers))
    return wrote


async def test_a_conflict_mark_never_revives_a_retired_peer(monkeypatch) -> None:
    wrote = await _marks(monkeypatch, {"p": "active", "q": "superseded"})
    assert wrote == [
        ("n1", {"status": "conflicted", "conflict_with": ["p"]}),
        ("p", {"status": "conflicted", "conflict_with": ["n1"]}),
    ]


async def test_a_mark_with_every_peer_retired_writes_nothing(monkeypatch) -> None:
    # The new row is not left conflicted against rows that no longer hold.
    assert await _marks(monkeypatch, {"q": "superseded", "erased": None}) == []


@pytest.mark.parametrize(
    ("sha", "status", "marked"),
    [
        ("judged", "active", "clear"),
        ("revised", "active", "moved"),
        ("judged", "superseded", "moved"),
    ],
)
async def test_a_deferred_mark_with_every_peer_retired_says_if_the_row_moved(
    monkeypatch, sha, status, marked
) -> None:
    # Nothing to flag: the row stands `clear`, or is moved when revised or retired since.
    import server.memory.store as memory_repo
    from server.memory.app.conflicts import mark_peers_conflicted

    rows = {
        "n1": SimpleNamespace(status=status, conflict_with=[], content_sha256=sha),
        "q": SimpleNamespace(status="superseded", conflict_with=[], content_sha256="q"),
    }

    async def get_by_id(mid, **kw):
        assert kw["strong"]
        return rows.get(mid)

    async def write(*_a, **_kw):
        raise AssertionError("nothing is written")

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(memory_repo, "write_versions_by_ids", write)
    landed = await mark_peers_conflicted(
        org="o", new_memory_id="n1", peer_ids=["q"], expected_sha256="judged"
    )
    assert landed == marked


async def test_a_peer_retiring_after_the_read_is_not_marked(monkeypatch) -> None:
    # The accepted window: the new row still names it, but it is never revived.
    wrote = await _marks(monkeypatch, {"p": "active"}, at_write={"p": "superseded"})
    assert wrote == [("n1", {"status": "conflicted", "conflict_with": ["p"]})]


async def test_a_late_verdict_leaves_a_retired_source_and_its_peers(monkeypatch) -> None:
    # Paris was superseded by London before the deferred verdict came back.
    assert await _marks(monkeypatch, {"p": "active"}, source="superseded") == []


def _index(state) -> None:
    # The id -> key map `_bulk_verdicts` builds once the rows that land are known.
    state.key_of = {r["memory_id"]: r.get("memory_key", "") for r in state.memory_rows}
