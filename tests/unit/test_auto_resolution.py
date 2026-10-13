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
        reason=ar.SAME_KEY_JUDGE,
    )
    (ev,) = emitted
    assert (ev["event_type"], ev["org"], ev["namespace"]) == ("memory.auto_supersede", "o", "ns")
    assert ev["actor"] == "process:loom"
    assert ev["subject"] == {
        "reason": "same_key_judge",
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


async def test_the_list_is_capped_and_the_count_is_not(emitted) -> None:
    rows = [_retired(f"m{i}") for i in range(250)]
    await ar.record_auto_supersessions(org="o", namespace="ns", retired=rows, reason="refines")
    (ev,) = emitted
    assert ev["subject"]["count"] == 250
    assert len(ev["subject"]["retired"]) == ar.MAX_ENTRIES


async def test_nothing_retired_writes_nothing(emitted) -> None:
    await ar.record_auto_supersessions(org="o", namespace="ns", retired=[], reason="refines")
    assert emitted == []


async def test_a_failing_audit_write_never_raises(monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("server.observability.audit.emit", boom)
    assert (
        await ar.record_auto_supersessions(
            org="o", namespace="ns", retired=[_retired("m1")], reason="refines"
        )
        is None
    )


def test_the_tracker_names_only_rows_it_was_asked_about_and_passed() -> None:
    t = ar.RetirementTracker(lambda row: row.memory_id == "locked")
    assert t.skip(SimpleNamespace(memory_id="ok")) is False
    assert t.skip(SimpleNamespace(memory_id="locked")) is True
    victims = [{"memory_id": m} for m in ("ok", "locked", "never_read")]
    assert [v["memory_id"] for v in t.retired(victims)] == ["ok"]


# --- the call sites -------------------------------------------------------------


def _fake_supersede(monkeypatch, readable: set[str]) -> None:
    """`supersede_duplicates` as the store runs it: `skip` is asked only about a row
    it could read."""
    from server.memory import store

    async def supersede(org, victims, *, ch=None, skip=None, strong=False):
        n = 0
        for v in victims:
            if v["memory_id"] in readable and not (
                skip and skip(SimpleNamespace(memory_id=v["memory_id"]))
            ):
                n += 1
        return n

    monkeypatch.setattr(store, "supersede_duplicates", supersede)


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


async def test_newest_wins_same_key_records_the_retired_not_the_locked(
    monkeypatch, emitted
) -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    _fake_supersede(monkeypatch, {"o1", "locked"})
    _lock(monkeypatch, "locked")
    new = [_live("n1")]
    ch = _CH(all_=[[new[0], _live("o1"), _live("locked"), _live("gone")]])
    n = await supersede_same_key_rows(
        org="o", namespace="ns", new_rows=new, ch=ch, policy="newest_wins"
    )
    assert n == 1
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
    assert emitted == []


async def test_same_key_judge_records_new_rows_and_peers_that_retired(monkeypatch, emitted) -> None:
    from server.memory.app import bulk_set, key_supersession
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_verdicts import BulkVerdictPlan
    from server.memory.app.key_supersession import JudgedRetirements, _victim

    async def no_same_key(**kw):
        return 0

    async def retire(org, victims, *, ch):
        # The locked peer stays live.
        return [v for v in victims if v["memory_id"] != "locked"]

    monkeypatch.setattr(key_supersession, "supersede_same_key_rows", no_same_key)
    monkeypatch.setattr(key_supersession, "retire_judged_peers", retire)
    state = _BulkWriteState()
    state.memory_rows = [
        {**_live("n1"), "status": "active"},
        {**_live("n2", "user_job"), "status": "superseded", "superseded_by": "p9"},
    ]

    class Input:
        org, namespace, detect_updates = "o", "ns", False

    judged = JudgedRetirements(
        new_rows=[("n2", "p9")],
        peers=[
            {**_victim(_live("o1"), "n1"), "label": "elaboration"},
            {**_victim(_live("locked"), "n1"), "label": "contradiction"},
        ],
        new_row_labels={"n2": "contradiction"},
    )
    await bulk_set._bulk_post_insert_links(None, Input(), state, judged, BulkVerdictPlan())
    (ev,) = emitted
    assert ev["subject"]["reason"] == "same_key_judge"
    by_id = {e["memory_id"]: e for e in ev["subject"]["retired"]}
    assert set(by_id) == {"n2", "o1"}
    assert by_id["n2"]["superseded_by"] == "p9" and by_id["n2"]["label"] == "contradiction"
    assert by_id["o1"]["superseded_by"] == "n1" and by_id["o1"]["label"] == "elaboration"
    assert "value" not in by_id["o1"]


async def test_the_judge_labels_each_retirement() -> None:
    from server.memory.app.key_supersession import judged_victims

    def row(mid: str, day: int) -> dict:
        return {**_live(mid), "status": "active", "updated_at": datetime(2026, 5, day)}

    out = judged_victims([row("n1", 20)], {"n1": [row("o1", 10)]}, {("n1", "o1"): "elaboration"})
    assert [(v["memory_id"], v["label"]) for v in out.peers] == [("o1", "elaboration")]
    out = judged_victims([row("n1", 10)], {"n1": [row("p1", 20)]}, {("n1", "p1"): "contradiction"})
    assert out.new_rows == [("n1", "p1")] and out.new_row_labels == {"n1": "contradiction"}


async def test_intra_batch_newest_wins_records_the_earlier_row(emitted) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    state = _BulkWriteState()
    state.memory_rows = [_live("old", "capital_a"), _live("new", "capital_b")]

    class Input:
        org, namespace = "o", "ns"

    await bulk_set._record_intra_batch_retirements(Input(), state, [("old", "new")])
    (ev,) = emitted
    assert ev["subject"]["reason"] == "newest_wins_contradiction" and _ids(ev) == ["old"]
    assert ev["subject"]["retired"][0]["superseded_by_key"] == "capital_b"
    await bulk_set._record_intra_batch_retirements(Input(), state, [])
    assert len(emitted) == 1


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


async def _refines(monkeypatch, relation: str) -> None:
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
        refined_peers=[_peer("o1"), _peer("locked")],
        ch=object(),
        relation=relation,
        new_value="lives in a city by the sea",
    )


@pytest.mark.parametrize(
    ("relation", "reason"),
    [("refines", "refines"), ("supersedes", "newest_wins_contradiction")],
)
async def test_refines_records_the_victims_it_superseded(
    monkeypatch, emitted, relation, reason
) -> None:
    _fake_supersede(monkeypatch, {"o1", "locked"})
    _lock(monkeypatch, "locked")
    await _refines(monkeypatch, relation)
    (ev,) = emitted
    assert ev["subject"]["reason"] == reason and _ids(ev) == ["o1"]
    assert ev["subject"]["retired"][0]["superseded_by_key"] == "user_city"


async def test_a_failed_refines_write_records_nothing(monkeypatch, emitted) -> None:
    from server.memory import store

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(store, "supersede_duplicates", boom)
    _lock(monkeypatch)
    await _refines(monkeypatch, "refines")
    assert emitted == []


async def test_value_update_records_the_older_rows_it_re_emitted(monkeypatch, emitted) -> None:
    from server.memory import store
    from server.memory.app import value_updates as vu
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_write_candidate_batch", 0)

    async def peers(*a, **k):
        return []

    def links(new_id, new_key, *a, **k):
        old = "gone" if new_key == "k2" else "o1"
        return [vu.UpdateLink(old, new_id, f"old_{new_key}", new_key, 0.9)]

    async def write(org, ids, update, *, ch=None):
        for mid in ids:
            if mid != "gone":
                update(SimpleNamespace(**{**_live(mid, "old_k1"), "superseded_by": ""}))

    monkeypatch.setattr(vu, "_peers", peers)
    monkeypatch.setattr(vu, "decide_links", links)
    monkeypatch.setattr(store, "write_versions_by_ids", write)
    rows = [
        {"memory_id": "n1", "memory_key": "k1", "value": "x", "valid_at": datetime.now(UTC)},
        {"memory_id": "n2", "memory_key": "k2", "value": "y", "valid_at": None},
    ]
    report = await vu.link_value_updates(
        org="o",
        namespace="ns",
        rows=rows,
        embeddings_by_key={"k1": [0.1], "k2": [0.2]},
        ch=object(),
    )
    assert len(report.links) == 2
    (ev,) = emitted
    assert ev["subject"]["reason"] == "value_update" and _ids(ev) == ["o1"]
    entry = ev["subject"]["retired"][0]
    assert (entry["superseded_by"], entry["superseded_by_key"]) == ("n1", "k1")
    assert entry["memory_key"] == "old_k1" and "value" not in entry


async def test_newest_wins_triple_repair_records_the_retired_carrier(monkeypatch, emitted) -> None:
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    async def policy(*a, **k):
        return "newest_wins"

    monkeypatch.setattr(proj._conflicts, "namespace_conflict_policy", policy)
    _fake_supersede(monkeypatch, {"m-old", "m-locked"})
    _lock(monkeypatch, "m-locked")
    links = [
        {"subject": "g", "relation": "r", "object": "old", "memory_key": "k_old"},
        {"subject": "g", "relation": "r", "object": "old", "memory_key": "k_locked"},
        {"subject": "g", "relation": "r", "object": "new", "memory_key": "k_new"},
    ]
    mems = [
        {**_live("m-old", "k_old")},
        {**_live("m-locked", "k_locked")},
        {**_live("m-new", "k_new")},
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
    assert n == 1
    (ev,) = emitted
    assert ev["subject"]["reason"] == "newest_wins_contradiction" and _ids(ev) == ["m-old"]
    assert ev["subject"]["retired"][0]["superseded_by_key"] == "k_new"
