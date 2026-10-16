"""A memory key names one attribute; under `newest_wins` the
newest row for a key is the live one, whatever session wrote it, and under the
default policy the conflict judge decides whether two sessions' rows of one key
are one attribute."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.keys import strip_variant_suffixes
from server.memory.app.key_supersession import (
    judged_victims,
    same_key_peers,
    same_key_victims,
    stamp_new_row_retirements,
    strip_variant_suffix,
)
from server.memory.domain.extracted_facts import keep_latest_per_key
from server.memory.domain.temporal_sql import LIVE_STATUSES


def _live(mid: str, key: str, session: str, *, pinned: bool = False) -> dict:
    return {
        "memory_id": mid,
        "memory_key": key,
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": session,
        "pinned": pinned,
    }


def test_older_rows_with_the_same_key_retire_behind_the_new_one() -> None:
    new = [{"memory_id": "n1", "memory_key": "apple_ceo"}]
    live = [
        _live("o1", "apple_ceo", "chunk-00000"),
        _live("n1", "apple_ceo", "chunk-00005"),  # the new row itself
        _live("o2", "abb_group_ceo", "chunk-00009"),  # another key: untouched
    ]
    victims = same_key_victims(new, live)
    assert [(v["memory_id"], v["superseded_by"], v["session_id"]) for v in victims] == [
        ("o1", "n1", "chunk-00000")
    ]


def test_pinned_rows_never_retire_and_the_last_new_row_for_a_key_is_the_newest() -> None:
    new = [{"memory_id": "n1", "memory_key": "k"}, {"memory_id": "n2", "memory_key": "k"}]
    live = [
        _live("o1", "k", "s1"),
        _live("p1", "k", "s2", pinned=True),
        _live("n1", "k", "s3"),
        _live("n2", "k", "s3"),
    ]
    victims = same_key_victims(new, live)
    assert [(v["memory_id"], v["superseded_by"]) for v in victims] == [("o1", "n2")]
    assert same_key_victims([], live) == []
    assert same_key_victims(new, []) == []


def test_variant_suffixes_fold_back_onto_the_attribute_key() -> None:
    assert (
        strip_variant_suffix("andy_warhol_death_location_alternate") == "andy_warhol_death_location"
    )
    assert strip_variant_suffix("user_pr_status_update") == "user_pr_status"
    assert strip_variant_suffix("user_employer_new") == "user_employer"
    # a closed-window fact keeps its own key; two-part keys are whole keys
    assert strip_variant_suffix("user_employer_former") == "user_employer_former"
    assert strip_variant_suffix("user_current") == "user_current"
    facts = [
        ExtractedFact("apple_ceo_updated", "Apple's CEO is Tim Cook."),
        ExtractedFact("apple_ceo", "Apple's CEO is Tim Cook."),
    ]
    out = strip_variant_suffixes(facts)
    assert [f.memory_key for f in out] == ["apple_ceo", "apple_ceo"]
    assert out[1] is facts[1]  # untouched facts are the same object


def test_in_batch_collision_keeps_the_later_statement() -> None:
    facts = [
        {"key": "amy_winehouse_death_location", "value": "Amy Winehouse died in Camden Town."},
        {"key": "hines_ward_position", "value": "Hines Ward plays wide receiver."},
        {"key": "amy_winehouse_death_location", "value": "Amy Winehouse died in Sofia."},
        {"key": "", "value": "no key"},
    ]
    kept, dropped = keep_latest_per_key(facts)
    assert [f["value"] for f in kept] == [
        "Hines Ward plays wide receiver.",
        "Amy Winehouse died in Sofia.",
        "no key",
    ]
    assert dropped == ["amy_winehouse_death_location"]
    kept2, dropped2 = keep_latest_per_key(kept)
    assert kept2 == kept and dropped2 == []
    _ = replace  # ExtractedFact is a dataclass; replace is what strip_variant_suffixes uses


def test_same_key_victims_carries_value() -> None:
    from server.memory.app.key_supersession import same_key_victims

    new = [{"memory_id": "n1", "memory_key": "k", "value": "new"}]
    live = [
        {
            "memory_id": "o1",
            "memory_key": "k",
            "namespace": "ns",
            "agent": "a",
            "user_id": "-",
            "session_id": "s1",
            "pinned": False,
            "value": "old",
        }
    ]
    victims = same_key_victims(new, live)
    assert victims and victims[0]["value"] == "old"


def _row(
    mid: str,
    valid_at: datetime | None,
    *,
    user: str = "-",
    agent: str = "a",
    pinned: bool = False,
    status: str = "active",
    created_at: datetime = datetime(2026, 10, 13, 8, 0),
    updated_at: datetime | None = None,
    version: int = 1,
) -> dict:
    """A row shaped like a bulk row: datetimes, `pinned` as 0/1."""
    return {
        **_live(mid, "user_ethnicity", f"s-{mid}"),
        "agent": agent,
        "user_id": user,
        "pinned": 1 if pinned else 0,
        "status": status,
        "value": f"value of {mid}",
        "valid_at": valid_at,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "version": version,
        "superseded_by": "",
        "invalid_at": None,
    }


def _d(day: int) -> datetime:
    return datetime(2023, 5, day)


def _pairs(out) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    return out.new_rows, [(v["memory_id"], v["superseded_by"]) for v in out.peers]


def test_same_key_peers_groups_the_read_by_key_newest_first() -> None:
    new = [_row("n1", _d(30)), {**_row("n2", _d(30)), "memory_key": "user_job"}]
    live = [
        _row("o1", _d(23)),
        _row("o2", _d(27)),
        {**_row("o3", _d(26)), "memory_key": "user_hometown"},
    ]
    peers = same_key_peers(new, live)
    assert {k: [p["memory_id"] for p in v] for k, v in peers.items()} == {"n1": ["o2", "o1"]}


async def test_a_new_row_the_batch_already_retired_or_marked_duplicate_is_not_judged(
    monkeypatch,
) -> None:
    for status in ("superseded", "duplicate"):
        seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
        await _judge([_row("n1", _d(30), status=status)], [_row("o1", _d(23))], seen)
        assert seen["queries"] == [] and seen["judged"] == []
    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    await _judge([_row("n1", _d(30), status="conflicted")], [_row("o1", _d(23))], seen)
    assert seen["judged"] == [("value of n1", ["o1"])]


def test_a_contradiction_retires_the_older_row_of_the_pair() -> None:
    new = [_row("n1", _d(30))]
    out = judged_victims(new, {"n1": [_row("o1", _d(23))]}, {("n1", "o1"): "contradiction"})
    assert _pairs(out) == ([], [("o1", "n1")])
    # An old session ingested after the current one: the new row lands superseded and
    # retires nothing itself, not even an older peer it also contradicts.
    late = [_row("n2", _d(20))]
    peers = {"n2": [_row("o1", _d(23)), _row("o0", _d(10))]}
    labels = {("n2", "o1"): "contradiction", ("n2", "o0"): "contradiction"}
    assert _pairs(judged_victims(late, peers, labels)) == ([("n2", "o1")], [])
    # ...unless it is pinned: then both stay, and the older peer still retires.
    pinned = [_row("n3", _d(20), pinned=True)]
    peers = {"n3": [_row("o1", _d(23)), _row("o0", _d(10))]}
    labels = {("n3", "o1"): "contradiction", ("n3", "o0"): "contradiction"}
    assert _pairs(judged_victims(pinned, peers, labels)) == ([], [("o0", "n3")])


def test_an_elaboration_retires_only_an_older_peer_and_other_labels_keep_both() -> None:
    new = [_row("n1", _d(25))]
    older, newer = _row("o1", _d(23)), _row("o2", _d(27))
    out = judged_victims(
        new, {"n1": [newer, older]}, {("n1", "o1"): "elaboration", ("n1", "o2"): "elaboration"}
    )
    assert _pairs(out) == ([], [("o1", "n1")])
    for label in ("restatement", "related", "unrelated"):
        assert _pairs(judged_victims(new, {"n1": [older]}, {("n1", "o1"): label})) == ([], [])
    assert _pairs(judged_victims(new, {"n1": [older]}, {})) == ([], [])


def test_a_tie_in_valid_time_has_one_older_row_whichever_side_judges() -> None:
    a = _row("a", _d(25), created_at=datetime(2026, 10, 13, 8, 0))
    b = _row("b", _d(25), created_at=datetime(2026, 10, 13, 8, 1))
    # Whether a judges b or b judges a, `a` (written first) is the one that retires.
    assert _pairs(judged_victims([a], {"a": [b]}, {("a", "b"): "contradiction"})) == (
        [("a", "b")],
        [],
    )
    assert _pairs(judged_victims([b], {"b": [a]}, {("b", "a"): "contradiction"})) == (
        [],
        [("a", "b")],
    )
    # Without valid time, both sides fall back to when the row was written.
    a, b = {**a, "valid_at": None}, {**b, "valid_at": None}
    assert _pairs(judged_victims([b], {"b": [a]}, {("b", "a"): "elaboration"})) == (
        [],
        [("a", "b")],
    )


def test_valid_times_with_an_offset_compare_in_utc() -> None:
    # 01:00 at -07:00 is 08:00 UTC: later than a peer at 05:00 UTC.
    new = [_row("n1", datetime(2023, 5, 30, 1, 0, tzinfo=timezone(timedelta(hours=-7))))]
    peer = _row("o1", datetime(2023, 5, 30, 5, 0))
    assert _pairs(judged_victims(new, {"n1": [peer]}, {("n1", "o1"): "contradiction"})) == (
        [],
        [("o1", "n1")],
    )
    as_text = [{**new[0], "valid_at": "2023-05-30T01:00:00-07:00"}]
    assert _pairs(judged_victims(as_text, {"n1": [peer]}, {("n1", "o1"): "contradiction"})) == (
        [],
        [("o1", "n1")],
    )


def test_a_retired_new_row_lands_superseded_on_both_rows() -> None:
    rows = [
        _row("n1", _d(20), version=7),
        {**_row("n2", _d(21)), "memory_key": "user_hometown"},
    ]

    def emb(r: dict, version: int) -> dict:
        ident = {c: r[c] for c in ("memory_key", "agent", "user_id", "session_id")}
        return {**ident, "version": version, "status": "active", "invalid_at": None}

    # A second embedding row of n1's identity at another version is not n1's.
    embs = [emb(rows[0], 7), emb(rows[0], 9), emb(rows[1], 1)]
    now = datetime(2026, 10, 13, 9, 0)
    stamp_new_row_retirements(rows, embs, [("n1", "o1")], now)
    # `invalid_at` is when the record retired the row, as for a stored row.
    assert (rows[0]["status"], rows[0]["superseded_by"], rows[0]["invalid_at"]) == (
        "superseded",
        "o1",
        now,
    )
    assert (embs[0]["status"], embs[0]["invalid_at"]) == ("superseded", now)
    assert embs[1]["status"] == "active"
    # The other row keeps its state but carries the same columns.
    assert (rows[1]["status"], rows[1]["superseded_by"], rows[1]["invalid_at"]) == (
        "active",
        "",
        None,
    )
    assert embs[2]["status"] == "active"


def _wire_judged(
    monkeypatch, labels: dict[str, str], *, judge_on: bool = True, policy: str = "auto_reinforce"
) -> dict:
    from server.memory.app import conflicts
    from server.memory.app.conflict_judge import Judgement
    from server.settings import settings

    seen: dict = {"queries": [], "judged": [], "in_flight": 0, "max_in_flight": 0}

    async def judge_peers(new_value, peers):
        seen["in_flight"] += 1
        seen["max_in_flight"] = max(seen["max_in_flight"], seen["in_flight"])
        await asyncio.sleep(0)
        seen["in_flight"] -= 1
        seen["judged"].append((new_value, [p.memory_id for p in peers]))
        return Judgement(verdict="conflict", ran=True, labels=labels, stated=labels)

    async def namespace_conflict_policy(org, namespace, ch=None):
        return policy

    monkeypatch.setattr(settings, "memory_conflict_judge", judge_on)
    monkeypatch.setattr(conflicts, "judge_peers", judge_peers)
    monkeypatch.setattr(conflicts, "namespace_conflict_policy", namespace_conflict_policy)
    return seen


def _as_reread(sql: str, rows: list[dict]) -> list[dict]:
    """The winner re-read selects the id as `mid`."""
    if "AS mid" in sql:
        return [{"mid": r["memory_id"], "version": r.get("version")} for r in rows]
    return rows


class _CH:
    def __init__(self, rows: list[dict], seen: dict) -> None:
        self.rows, self.seen = rows, seen

    async def query_all(self, sql: str, params: dict) -> list[dict]:
        self.seen["queries"].append(params)
        self.seen["sql"] = sql
        return _as_reread(sql, self.rows)


async def _judge(rows: list[dict], live: list[dict], seen: dict, embs: list[dict] | None = None):
    from server.memory.app.key_supersession import judge_same_key_rows

    return await judge_same_key_rows(
        org="o",
        namespace="ns",
        memory_rows=rows,
        embedding_rows=embs or [],
        ch=_CH(live, seen),
        now=datetime(2026, 10, 13, 9, 0),
    )


async def test_the_judge_reads_the_newest_rows_of_each_key_and_user_only(monkeypatch) -> None:
    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    rows = [_row("n1", _d(30), user="u")]
    out = await _judge(rows, [_row("o1", _d(23), user="u")], seen)
    assert _pairs(out) == ([], [("o1", "n1")])
    assert seen["judged"] == [("value of n1", ["o1"])]
    (params,) = seen["queries"]
    assert params["keys"] == ["user_ethnicity"] and params["own_user"] == "u"
    assert params["ids"] == ["n1"] and params["n"] == 3


async def test_a_high_sensitivity_row_never_reaches_the_judge(monkeypatch) -> None:
    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    rows = [{**_row("n1", _d(20)), "sensitivity": "high"}]
    out = await _judge(rows, [_row("o1", _d(23))], seen)
    assert _pairs(out) == ([], []) and seen["queries"] == [] and seen["judged"] == []
    # The stored side is filtered in the read.
    await _judge([_row("n2", _d(30))], [], seen)
    assert "sensitivity != 'high'" in seen["sql"]


async def test_a_late_old_session_lands_superseded(monkeypatch) -> None:
    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    rows = [_row("n1", _d(20))]
    out = await _judge(rows, [_row("o1", _d(23))], seen)
    assert out.new_rows == [("n1", "o1")] and out.peers == []
    assert (rows[0]["status"], rows[0]["superseded_by"]) == ("superseded", "o1")


async def test_a_peer_the_judge_left_unlabelled_stays_live(monkeypatch) -> None:
    from server.memory.app import conflicts
    from server.memory.app.conflict_judge import Judgement

    seen = _wire_judged(monkeypatch, {})

    async def judge_peers(new_value, peers):
        # The flag path's fold reads the unlabelled o1 as a contradiction; a
        # retirement must not.
        return Judgement(
            verdict="conflict",
            ran=True,
            labels={"o1": "contradiction", "o2": "unrelated"},
            stated={"o2": "unrelated"},
        )

    monkeypatch.setattr(conflicts, "judge_peers", judge_peers)
    out = await _judge([_row("n1", _d(30))], [_row("o1", _d(23)), _row("o2", _d(25))], seen)
    assert _pairs(out) == ([], [])


async def test_judge_calls_are_bounded_per_bulk(monkeypatch) -> None:
    from server.settings import settings

    seen = _wire_judged(monkeypatch, {})
    monkeypatch.setattr(settings, "bulk_conflict_check_concurrency", 2)
    monkeypatch.setattr(settings, "bulk_conflict_check_max_entries", 5)
    rows = [{**_row(f"n{i}", _d(30)), "memory_key": f"k{i}"} for i in range(8)]
    live = [{**_row(f"o{i}", _d(23)), "memory_key": f"k{i}"} for i in range(8)]
    await _judge(rows, live, seen)
    assert len(seen["judged"]) == 5 and seen["max_in_flight"] <= 2
    assert len(seen["queries"][0]["keys"]) == 5


async def test_another_policy_or_the_judge_off_reads_nothing(monkeypatch) -> None:
    for kw in (
        {"policy": "newest_wins"},
        {"policy": "manual_review"},
        {"policy": "append_only"},
        {"judge_on": False},
    ):
        seen = _wire_judged(monkeypatch, {"o1": "contradiction"}, **kw)
        rows = [_row("n1", _d(20))]
        out = await _judge(rows, [_row("o1", _d(23))], seen)
        assert _pairs(out) == ([], []) and seen["queries"] == [] and rows[0]["status"] == "active"


async def test_a_failed_read_retires_nothing(monkeypatch) -> None:
    from server.memory.app.key_supersession import judge_same_key_rows

    _wire_judged(monkeypatch, {"o1": "contradiction"})

    class Boom:
        async def query_all(self, sql, params):
            raise RuntimeError("down")

    rows = [_row("n1", _d(20))]
    out = await judge_same_key_rows(
        org="o",
        namespace="ns",
        memory_rows=rows,
        embedding_rows=[],
        ch=Boom(),
        now=datetime.now(UTC),
    )
    assert _pairs(out) == ([], []) and rows[0]["status"] == "active"


async def test_bulk_drops_a_retired_new_row_from_the_settling_steps(monkeypatch) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_verdicts import BulkVerdictPlan

    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    from server.memory.api.models_bulk import _BulkSetResult

    state = _BulkWriteState()
    state.results = [
        _BulkSetResult(memory_key="user_ethnicity", version=1),
        _BulkSetResult(memory_key="user_hometown", version=1),
    ]
    state.memory_rows = [
        _row("n1", _d(20), status="conflicted"),
        {**_row("n2", _d(21)), "memory_key": "user_hometown"},
    ]
    _index(state)
    plan = BulkVerdictPlan()
    plan.conflict_marks = [("n1", ["x"]), ("n2", ["y"])]
    plan.refines = [
        ("n1", "user_ethnicity", [], "supersedes", "v"),
        ("n2", "user_hometown", [], "supersedes", "v"),
    ]
    plan.conflicted = ["user_ethnicity", "user_hometown"]

    class Input:
        org, namespace = "o", "ns"

    import server.memory.app.key_supersession as ks

    real = ks.judge_same_key_rows

    async def judge(**kw):
        return await real(**{**kw, "ch": _CH([_row("o1", _d(23))], seen)})

    monkeypatch.setattr(ks, "judge_same_key_rows", judge)
    out = await bulk_set._bulk_judge_same_key(None, Input(), state, plan)
    assert out.new_rows == [("n1", "o1")]
    assert plan.conflict_marks == [("n2", ["y"])]
    assert [r[0] for r in plan.refines] == ["n2"]
    assert plan.conflicted == ["user_hometown"]
    assert [(r.memory_key, r.superseded_by) for r in state.results] == [
        ("user_ethnicity", "o1"),
        ("user_hometown", ""),
    ]
    assert state.memory_rows[0]["status"] == "superseded"
    # A row that landed superseded is not followed: a brand-new id held no graph facts,
    # and a stored row revised in place closes what it held through its reconcile.
    assert state.judged_follow_pairs == []


async def test_bulk_retires_judged_peers_after_the_insert_and_links_only_live_rows(
    monkeypatch,
) -> None:
    from server.memory.app import bulk_set, key_supersession, value_updates
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.key_supersession import JudgedRetirements, _victim

    calls: dict = {}
    emitted: list = []

    async def emit(event_type, **kw):
        emitted.append(event_type)
        return "h"

    # The retirement's audit record, not the ClickHouse insert behind it.
    monkeypatch.setattr("server.observability.audit.emit", emit)

    async def no_same_key(**kw):
        return 0

    async def retire(org, victims, *, ch):
        calls["retired"] = [v["memory_id"] for v in victims]
        return victims

    async def link(**kw):
        calls["linked"] = [r["memory_id"] for r in kw["rows"]]
        return type("R", (), {"links": []})()

    monkeypatch.setattr(key_supersession, "supersede_same_key_rows", no_same_key)
    monkeypatch.setattr(key_supersession, "retire_judged_peers", retire)
    monkeypatch.setattr(value_updates, "link_value_updates", link)
    state = _BulkWriteState()
    from server.memory.api.models_bulk import _BulkSetResult

    state.results = [_BulkSetResult(memory_key="user_ethnicity", version=1)]
    state.memory_rows = [
        _row("n1", _d(30)),
        {**_row("n2", _d(20)), "status": "superseded"},
        # The lower version of a key whose kept row the judge retired.
        {**_row("n3", _d(20)), "memory_key": "user_hometown"},
    ]
    _index(state)
    state.judged_retired_keys = {"user_hometown"}

    class Input:
        org, namespace, detect_updates = "o", "ns", True

    from server.memory.app.bulk_verdicts import BulkVerdictPlan

    plan = BulkVerdictPlan()
    # Another key's verdicts flagged and refined o1: once retired, it leaves them.
    plan.conflict_marks = [("n9", ["o1", "x"]), ("n8", ["o1"])]
    plan.refines = [
        ("n7", "k", [type("P", (), {"memory_id": "o1"})()], "supersedes", "v"),
        ("n6", "k", [type("P", (), {"memory_id": "y"})()], "supersedes", "v"),
    ]
    judged = JudgedRetirements(peers=[_victim(_row("o1", _d(23)), "n1")])
    await bulk_set._bulk_post_insert_links(None, Input(), state, judged, plan)
    assert plan.conflict_marks == [("n9", ["x"])]
    assert [r[0] for r in plan.refines] == ["n6"]
    assert calls == {"retired": ["o1"], "linked": ["n1"]}
    # The entry that retired it names it.
    assert [(r.memory_key, r.retired) for r in state.results] == [("user_ethnicity", ["o1"])]
    assert state.judged_follow_pairs == [("o1", "n1")]
    assert emitted == ["memory.auto_supersede"]


def test_a_revision_is_ordered_by_when_it_was_written_not_when_the_key_was_created() -> None:
    # S1 created the key on 05-01 and restates it on 06-01; S2 wrote it on 05-20. No
    # valid time: the 06-01 statement is the later one, though its created_at is 05-01.
    s1 = _row(
        "s1", None, created_at=datetime(2026, 5, 1), updated_at=datetime(2026, 6, 1), version=2
    )
    s2 = _row("s2", None, created_at=datetime(2026, 5, 20))
    out = judged_victims([s1], {"s1": [s2]}, {("s1", "s2"): "contradiction"})
    assert _pairs(out) == ([], [("s2", "s1")])


def test_microseconds_do_not_decide_the_order() -> None:
    # The stored row has whole seconds, the new one microseconds: a tie, so the id decides.
    a = _row("a", None, created_at=datetime(2026, 10, 13, 8, 0, 0, 999999))
    b = _row("b", None, created_at=datetime(2026, 10, 13, 8, 0, 0))
    out = judged_victims([a], {"a": [b]}, {("a", "b"): "contradiction"})
    assert _pairs(out) == ([("a", "b")], [])


def test_a_row_whose_amounts_the_newer_row_drops_stays() -> None:
    old = {**_row("o1", _d(20)), "value": "ordered from Thrive Market and spent around $150"}
    new = [{**_row("n1", _d(27)), "value": "ordered from Thrive Market and got great deals"}]
    for label in ("elaboration", "contradiction"):
        assert _pairs(judged_victims(new, {"n1": [old]}, {("n1", "o1"): label})) == ([], [])
    # Nor does a new row retire behind a newer peer that drops its amount.
    late = [{**old, "memory_id": "n2", "valid_at": _d(10)}]
    newer = {**new[0], "memory_id": "o2"}
    assert _pairs(judged_victims(late, {"n2": [newer]}, {("n2", "o2"): "contradiction"})) == (
        [],
        [],
    )
    # An amount the newer row restates is kept, so the older row retires.
    kept = [{**_row("n3", _d(27)), "value": "ordered from Thrive Market, spent $150, great deals"}]
    out = judged_victims(kept, {"n3": [old]}, {("n3", "o1"): "elaboration"})
    assert _pairs(out) == ([], [("o1", "n3")])


async def test_only_the_batch_winner_of_a_key_is_judged(monkeypatch) -> None:
    # Two entries of one key are two versions of one row: FINAL keeps the higher.
    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    lower = _row("n1", datetime(2026, 9, 1), version=1)
    higher = _row("n2", datetime(2024, 1, 1), version=2)
    out = await _judge([lower, higher], [_row("o1", datetime(2025, 1, 1))], seen)
    assert seen["judged"] == [("value of n2", ["o1"])]
    assert _pairs(out) == ([("n2", "o1")], [])
    assert (lower["status"], higher["status"]) == ("active", "superseded")


async def test_the_triple_plane_follows_judged_retirements_first(monkeypatch) -> None:
    from server.memory.app import bulk_set, entity_projections
    from server.memory.app.bulk_verdicts import BulkVerdictPlan
    from server.memory.app.search.clock import StageClock

    seen: list = []

    async def follow(input, pairs, *, ch=None, hops=0):
        seen.extend(pairs)
        return entity_projections.FollowOutcome(0, 0, [])

    monkeypatch.setattr(entity_projections, "follow_row_supersessions", follow)
    await bulk_set._bulk_follow_row_supersessions(
        None,
        object(),
        BulkVerdictPlan(),
        [],
        [("i1", "i2")],
        StageClock({}),
        judged_pairs=[("j1", "j2")],
    )
    assert seen == [("j1", "j2"), ("i1", "i2")]


def test_a_late_dated_session_does_not_bury_an_undated_current_value() -> None:
    # "Lives in Berlin", written 2026-09-01 with no valid time; an old 2024 session
    # ingested today says "Paris", dated 2024-03-01. Berlin is the later statement.
    cur = _row("cur", None, created_at=datetime(2026, 9, 1))
    late = _row("late", datetime(2024, 3, 1), created_at=datetime(2026, 10, 13, 9, 0))
    out = judged_victims([late], {"late": [cur]}, {("late", "cur"): "contradiction"})
    assert _pairs(out) == ([("late", "cur")], [])
    # An undated late row ranks by its write time: the later statement.
    undated = _row("u", None, created_at=datetime(2026, 10, 13, 9, 0))
    dated = _row("d", datetime(2025, 6, 1), created_at=datetime(2026, 10, 1))
    out = judged_victims([undated], {"u": [dated]}, {("u", "d"): "contradiction"})
    assert _pairs(out) == ([], [("d", "u")])


def test_peers_sort_the_same_whatever_order_they_arrive_in() -> None:
    from itertools import permutations

    rows = [
        _row("a", datetime(2020, 1, 1), created_at=datetime(2026, 3, 1)),
        _row("b", None, created_at=datetime(2026, 2, 1)),
        _row("c", datetime(2024, 1, 1), created_at=datetime(2026, 1, 1)),
    ]
    new = [_row("n", None, created_at=datetime(2026, 10, 13))]
    orders = {
        tuple(p["memory_id"] for p in same_key_peers(new, list(perm))["n"])
        for perm in permutations(rows)
    }
    assert orders == {("b", "c", "a")}


async def test_the_peer_read_orders_by_the_same_key_as_python(monkeypatch) -> None:
    seen = _wire_judged(monkeypatch, {})
    await _judge([_row("n1", _d(30))], [], seen)
    sql = " ".join(seen["sql"].split())
    order = "ORDER BY least(coalesce(valid_at, updated_at), updated_at) DESC, updated_at DESC"
    assert f"{order}, toString(memory_id) DESC" in sql
    assert "version" in seen["sql"].split("FROM")[0]


class _StoredRow:
    def __init__(
        self, mid: str, *, pinned=False, status="active", version=1, expires_at=None, valid_to=None
    ) -> None:
        self.memory_id, self.pinned, self.status, self.version = mid, pinned, status, version
        self.lock_mode, self.lock_until = "", None
        self.expires_at, self.valid_to = expires_at, valid_to


def _fake_store(monkeypatch, stored: dict[str, _StoredRow], *, fail: str = "") -> list:
    """`supersede_duplicates` as the store runs it: `skip` is asked only about a row
    it read, a row it cannot read is passed over and still counted, and `on_written`
    hears the rows its insert wrote. `fail="mirror"` raises after that insert (the
    embeddings mirror), `fail="insert"` before it."""
    from server.memory import store

    wrote: list = []

    async def supersede(
        org, victims, *, ch=None, skip=None, strong=False, on_written=None, on_unconfirmed=None
    ):
        assert strong, "the retirement reads read-your-writes"
        rows = [stored[v["memory_id"]] for v in victims if v["memory_id"] in stored]
        rows = [r for r in rows if not skip(r)]
        if fail == "insert":
            raise RuntimeError("insert down")
        wrote.extend(r.memory_id for r in rows)
        if on_written is not None:
            on_written([{"memory_id": r.memory_id, "version": 9} for r in rows])
        if fail == "mirror":
            raise RuntimeError("mirror down")
        return len(victims)

    monkeypatch.setattr(store, "supersede_duplicates", supersede)
    return wrote


async def test_retire_judged_peers_reports_only_rows_it_wrote(monkeypatch) -> None:
    from server.memory.app.key_supersession import _victim, retire_judged_peers
    from server.memory.domain import lock

    def victim(mid: str) -> dict:
        return {**_victim(_row(mid, _d(20)), "n1"), "judged_version": 1}

    monkeypatch.setattr(lock, "refuses_retirement", lambda row: row.memory_id == "locked")
    stored = {
        "ok": _StoredRow("ok"),
        "locked": _StoredRow("locked"),
        "pinned": _StoredRow("pinned", pinned=True),
        "retired": _StoredRow("retired", status="superseded"),
        "revised": _StoredRow("revised", version=2),
        "expired": _StoredRow("expired", expires_at=datetime(2020, 1, 1)),
        "closed": _StoredRow("closed", valid_to=datetime(2020, 1, 1)),
    }
    wrote = _fake_store(monkeypatch, stored)
    names = ["ok", "locked", "pinned", "retired", "revised", "expired", "closed", "gone"]
    out = await retire_judged_peers("o", [victim(m) for m in names], ch=None)
    assert [v["memory_id"] for v in out] == ["ok"] and wrote == ["ok"]


async def test_retire_judged_peers_reports_nothing_on_a_failed_write(monkeypatch) -> None:
    from server.memory.app.key_supersession import _victim, retire_judged_peers

    _fake_store(monkeypatch, {"o1": _StoredRow("o1")}, fail="insert")
    assert await retire_judged_peers("o", [_victim(_row("o1", _d(20)), "n1")], ch=None) == []


async def test_retire_judged_peers_reports_the_written_rows_when_the_mirror_fails(
    monkeypatch,
) -> None:
    from server.memory.app.key_supersession import _victim, retire_judged_peers
    from server.memory.domain import lock

    monkeypatch.setattr(lock, "refuses_retirement", lambda row: row.memory_id == "locked")
    stored = {"o1": _StoredRow("o1"), "locked": _StoredRow("locked")}
    _fake_store(monkeypatch, stored, fail="mirror")
    victims = [_victim(_row(m, _d(20)), "n1") for m in ("o1", "locked")]
    # The superseded version of o1 is in loom.memory: it retired, though the write raised.
    out = await retire_judged_peers("o", victims, ch=None)
    assert [v["memory_id"] for v in out] == ["o1"]


async def test_a_key_written_twice_leaves_the_plan_by_key(monkeypatch) -> None:
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_verdicts import BulkVerdictPlan

    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    state = _BulkWriteState()
    # A key new to this identity, twice: two ids, one identity; FINAL keeps n2.
    state.memory_rows = [_row("n1", _d(10), version=1), _row("n2", _d(11), version=2)]
    _index(state)
    state.results = [
        _BulkSetResult(memory_key="user_ethnicity", version=1),
        _BulkSetResult(memory_key="user_ethnicity", version=2),
    ]
    plan = BulkVerdictPlan()
    plan.conflict_marks = [("n1", ["x"]), ("n2", ["y"])]
    plan.conflicted = ["user_ethnicity"]

    class Input:
        org, namespace = "o", "ns"

    import server.memory.app.key_supersession as ks

    real = ks.judge_same_key_rows

    async def judge(**kw):
        return await real(**{**kw, "ch": _CH([_row("o1", _d(23))], seen)})

    monkeypatch.setattr(ks, "judge_same_key_rows", judge)
    out = await bulk_set._bulk_judge_same_key(None, Input(), state, plan)
    assert out.new_rows == [("n2", "o1")]
    assert plan.conflict_marks == [] and plan.conflicted == []
    assert [r.superseded_by for r in state.results] == ["o1", "o1"]
    assert state.judged_retired_keys == {"user_ethnicity"}


async def test_the_peer_read_keeps_only_the_writers_subject(monkeypatch) -> None:
    # The same user's rows from other sessions; for the anonymous user, the same
    # agent's; never the writer's own stored row, which this write revises.
    seen = _wire_judged(monkeypatch, {})
    rows = [{**_row("n1", _d(30)), "agent": "a", "user_id": "u", "session_id": "s"}]
    await _judge(rows, [], seen)
    sql = " ".join(seen["sql"].split())
    assert "user_id = {own_user:String}" in sql
    assert "(user_id != '-' OR agent = {own_agent:String})" in sql
    assert "NOT (agent = {own_agent:String} AND session_id = {own_session:String})" in sql
    assert "LIMIT {n:UInt32} BY memory_key" in sql
    params = seen["queries"][0]
    assert (params["own_agent"], params["own_user"], params["own_session"]) == ("a", "u", "s")


def test_a_future_valid_time_ranks_as_when_it_was_written() -> None:
    # "Will move to Berlin" valid 2027 was stated on 10-01; the undated correction of
    # 10-13 is the later statement.
    future = _row("f", datetime(2027, 1, 1), created_at=datetime(2026, 10, 1))
    fix = _row("c", None, created_at=datetime(2026, 10, 13))
    out = judged_victims([fix], {"c": [future]}, {("c", "f"): "contradiction"})
    assert _pairs(out) == ([], [("f", "c")])


def test_a_valid_time_outside_the_datetime_range_ranks_as_undated() -> None:
    from server.memory.app.key_supersession import _rank

    # A row cannot store one (its insert fails), and a date outside it is no date.
    built = _row("x", datetime(1955, 4, 18), created_at=datetime(2026, 10, 13))
    assert _rank(built) == _rank({**built, "valid_at": None})


def test_results_say_what_the_judge_did() -> None:
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.api.models_common import MemorySetPeer
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_set import _settle_results
    from server.memory.app.bulk_verdicts import BulkVerdictPlan

    def res(key: str, verdict: str, peers: list[str]) -> _BulkSetResult:
        return _BulkSetResult(
            memory_key=key,
            version=1,
            conflict_verdict=verdict,
            peers=[MemorySetPeer(memory_id=p, memory_key="user_city") for p in peers],
            next="memory.update(...)",
        )

    keys = ["user_ethnicity", "user_heritage", "user_home", "user_age", "user_job", "user_pet"]
    state = _BulkWriteState()
    state.memory_rows = [{"memory_id": f"n-{k}", "memory_key": k} for k in keys]
    _index(state)
    state.results = [
        res("user_ethnicity", "conflict", ["q"]),
        res("user_heritage", "conflict", ["p"]),
        res("user_home", "conflict", ["p", "r"]),
        res("user_age", "conflict", ["s"]),
        # The caller may not read x: its result shows p only, its marks hold both.
        res("user_job", "conflict", ["p"]),
        # No read scope resolved: the result shows no peer, the marks hold p.
        res("user_pet", "conflict", []),
        # Another entry of user_ethnicity that wrote nothing (rejected): left as it is.
        _BulkSetResult(memory_key="user_ethnicity", version=0, conflict_verdict="rejected"),
    ]
    plan = BulkVerdictPlan()
    plan.conflicted = list(keys)
    marks = {"user_heritage": ["p"], "user_home": ["p", "r"], "user_age": ["s"]}
    marks |= {"user_job": ["p", "x"], "user_pet": ["p"]}
    # The new row of user_ethnicity landed behind q: its mark has gone already.
    plan.conflict_marks = [(f"n-{k}", v) for k, v in marks.items()]
    _settle_results(state, plan, behind={"user_ethnicity": "q"})
    assert state.results[-1].conflict_verdict == "rejected"
    assert state.results[-1].superseded_by == ""
    eth = state.results[0]
    assert (eth.conflict_verdict, eth.superseded_by, eth.peers, eth.next) == (
        "superseded",
        "q",
        [],
        "",
    )
    # Stored p retired; the plan's marks decide, as `_drop_retired_peers` left them.
    from server.memory.app.bulk_set import _drop_retired_peers

    _drop_retired_peers(plan, {"p"})
    _settle_results(state, plan, retired_peers={"p"})
    her, home, age, job, pet = state.results[1:6]
    assert (her.conflict_verdict, her.peers, her.next) == ("clear", [], "")
    assert home.conflict_verdict == "conflict" and [p.memory_id for p in home.peers] == ["r"]
    assert "user_city" in home.next
    assert age.conflict_verdict == "conflict" and age.next == "memory.update(...)"
    # x is still marked, so the row lands conflicted and the result says so.
    assert job.conflict_verdict == "conflict" and job.peers == []
    assert pet.conflict_verdict == "clear"
    assert plan.conflicted == ["user_home", "user_age", "user_job"]


def test_a_locked_new_row_does_not_land_superseded() -> None:
    # A no_delete lock lets a revision change the value, but refuses retirement.
    peer = _row("o1", _d(23))
    labels = {("n1", "o1"): "contradiction"}
    locked = [{**_row("n1", _d(20)), "lock_mode": "no_delete", "lock_until": None}]
    assert _pairs(judged_victims(locked, {"n1": [peer]}, labels)) == ([], [])
    until = datetime(2999, 1, 1)
    held = [{**_row("n1", _d(20)), "lock_mode": "read_only", "lock_until": until}]
    assert _pairs(judged_victims(held, {"n1": [peer]}, labels)) == ([], [])
    # A lapsed lock no longer protects it.
    lapsed = [{**_row("n1", _d(20)), "lock_mode": "no_delete", "lock_until": datetime(2000, 1, 1)}]
    assert _pairs(judged_victims(lapsed, {"n1": [peer]}, labels)) == ([("n1", "o1")], [])


class _RevisingCH(_CH):
    """The peer read returns the judged rows; the winner re-read returns them
    revised (another version), retired or gone."""

    def __init__(self, rows: list[dict], seen: dict, reread: list[dict]) -> None:
        super().__init__(rows, seen)
        self.reread = reread

    async def query_all(self, sql: str, params: dict) -> list[dict]:
        if "LIMIT {n:UInt32} BY" in sql:
            return await super().query_all(sql, params)
        self.seen["reread_sql"] = sql
        return _as_reread(sql, self.reread)


async def test_a_winner_revised_while_the_judge_ran_keeps_the_new_row_live(monkeypatch) -> None:
    from server.memory.app.key_supersession import judge_same_key_rows

    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})
    peer = _row("o1", _d(23))
    for reread in ([{**peer, "version": 2}], []):
        rows = [_row("n1", _d(20))]
        out = await judge_same_key_rows(
            org="o",
            namespace="ns",
            memory_rows=rows,
            embedding_rows=[],
            ch=_RevisingCH([peer], seen, reread),
            now=datetime(2026, 10, 13, 9, 0),
        )
        assert out.new_rows == [] and rows[0]["status"] == "active"
    # Unchanged: it lands superseded. The re-read admits only a live, current row.
    rows = [_row("n1", _d(20))]
    out = await judge_same_key_rows(
        org="o",
        namespace="ns",
        memory_rows=rows,
        embedding_rows=[],
        ch=_RevisingCH([peer], seen, [peer]),
        now=datetime(2026, 10, 13, 9, 0),
    )
    assert out.new_rows == [("n1", "o1")] and rows[0]["status"] == "superseded"
    for pred in (
        "expires_at IS NULL OR expires_at > now()",
        "valid_to IS NULL OR valid_to > now()",
    ):
        assert pred in seen["reread_sql"]


async def test_an_expired_or_closed_peer_is_never_read(monkeypatch) -> None:
    seen = _wire_judged(monkeypatch, {})
    await _judge([_row("n1", _d(30))], [], seen)
    sql = " ".join(seen["sql"].split())
    assert "(expires_at IS NULL OR expires_at > now())" in sql
    assert "(valid_to IS NULL OR valid_to > now())" in sql
    # Before the LIMIT: an expired row takes no peer slot.
    assert sql.index("expires_at IS NULL") < sql.index("LIMIT")


def test_only_entries_that_wrote_a_live_row_project_graph_facts() -> None:
    from types import SimpleNamespace

    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.bulk_set import _projected

    class Input(SimpleNamespace):
        def model_copy(self, *, update):
            return Input(**{**self.__dict__, **update})

    keys = ["user_city", "user_job", "user_secret", "user_pinned"]
    state = _BulkWriteState()
    # user_secret was refused by the blacklist and user_pinned held back (as an
    # unchanged restatement, an identity duplicate or a merge would be): both wrote
    # nothing (version 0) and keep their place in the results.
    state.results = [
        _BulkSetResult(memory_key=k, version=v, memory_id=f"id-{k}")
        for k, v in zip(keys, (1, 2, 0, 0), strict=True)
    ]
    inp = Input(entries=[SimpleNamespace(memory_key=k) for k in keys])

    def projected() -> tuple[list[str], list[str]]:
        p_input, p_results = _projected(inp, state)
        return [e.memory_key for e in p_input.entries], [r.memory_key for r in p_results]

    assert projected() == (["user_city", "user_job"], ["user_city", "user_job"])
    # A key the judge landed superseded projects nothing either.
    state.judged_retired_keys = {"user_city"}
    assert projected() == (["user_job"], ["user_job"])
    # Nor does a 'high' row: its content stays out of the shared graph and its replay.
    state.judged_retired_keys = set()
    state.memory_rows = [
        {"memory_id": "id-user_city", "version": 1, "sensitivity": "high"},
        {"memory_id": "id-user_job", "version": 2},
    ]
    _index(state)
    assert projected() == (["user_job"], ["user_job"])
    # Two entries of one stored key share its id: only the 'high' version is fenced.
    state.results = [
        _BulkSetResult(memory_key="user_city", version=v, memory_id="id-user_city") for v in (1, 2)
    ]
    inp.entries[:] = [SimpleNamespace(memory_key="user_city") for _ in range(2)]
    state.memory_rows = [
        {"memory_id": "id-user_city", "version": 1, "sensitivity": "high"},
        {"memory_id": "id-user_city", "version": 2},
    ]
    _index(state)
    assert projected() == (["user_city"], ["user_city"])


async def test_follow_pairs_past_the_cap_or_in_a_failed_follow_go_to_the_repair(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from server.memory.app import bulk_set, entity_projections
    from server.memory.app.bulk_verdicts import BulkVerdictPlan
    from server.memory.app.search.clock import StageClock

    followed: list = []
    recorded: list = []
    fail = {"now": False}
    unread = {"n": 0}

    async def follow(input, pairs, *, ch=None, hops=0):
        if fail["now"]:
            raise RuntimeError("follow down")
        followed.append(len(pairs))
        return entity_projections.FollowOutcome(0, 0, pairs[: unread["n"]])

    async def record(input, *, projection, payload, **kw):
        recorded.append((projection, len(payload["pairs"])))

    monkeypatch.setattr(entity_projections, "follow_row_supersessions", follow)
    monkeypatch.setattr(bulk_set, "record_input_projection_failure", record)
    monkeypatch.setattr(bulk_set, "bulk_source_resource_id", lambda input: "src")
    inp = SimpleNamespace(org="o", namespace="ns")
    pairs = [(f"o{i}", f"n{i}") for i in range(bulk_set._FOLLOW_MAX + 10)]

    async def run() -> None:
        await bulk_set._bulk_follow_row_supersessions(
            None, inp, BulkVerdictPlan(), [], [], StageClock({}), judged_pairs=pairs
        )

    await run()
    assert followed == [bulk_set._FOLLOW_MAX]
    assert recorded == [("triples_follow_rows", 10)]
    # A failed follow leaves every pair for the repair.
    fail["now"], recorded[:] = True, []
    await run()
    assert recorded == [("triples_follow_rows", len(pairs))]
    # So does a pair whose chain was too long to read.
    fail["now"], recorded[:], unread["n"] = False, [], 3
    await run()
    assert recorded == [("triples_follow_rows", 10 + 3)]


async def test_the_repair_follows_the_pairs_a_write_left(monkeypatch) -> None:
    from types import SimpleNamespace

    from server.memory.app import entity_projections, projection_failures
    from server.memory.domain.triples_follow_rows import FollowPlan

    chunks: list = []

    async def chunk(scope, pairs, *, ch=None, hops=0):
        chunks.append((scope.org, scope.namespace, len(pairs)))
        assert hops == entity_projections.REPAIR_CHAIN_HOPS
        return FollowPlan(closes=[], inserts=[]), pairs[: unread["n"]]

    unread = {"n": 0}

    marked: list[str] = []

    async def resolved(**kw):
        return 1

    async def abandoned(**kw):
        marked.append(kw["reason"])

    async def no_links(*a, **k):
        return []

    monkeypatch.setattr(entity_projections, "_plan_chunk", chunk)
    # The follow reads the losers' links after its plan; these rows have none.
    monkeypatch.setattr(entity_projections, "linked_triples", no_links)
    monkeypatch.setattr(projection_failures, "mark_projection_failures_resolved", resolved)
    monkeypatch.setattr(projection_failures, "mark_projection_failure_abandoned", abandoned)
    failure = SimpleNamespace(
        failure_id="f1",
        org="o",
        namespace="ns",
        agent="a",
        projection="triples_follow_rows",
        payload={"pairs": [[_uid(f"o{i}"), _uid(f"n{i}")] for i in range(70)]},
    )
    out = await projection_failures.retry_projection_failure(failure, ch=object())
    assert out.resolved and chunks == [("o", "ns", 64), ("o", "ns", 6)]
    # A chain even the repair cannot read (a cycle, or past its budget) never will be:
    # the failure is abandoned with its reason, neither resolved nor left to block the queue.
    unread["n"] = 1
    out = await projection_failures.retry_projection_failure(failure, ch=object())
    assert out.abandoned and not out.resolved
    assert out.reason == "unfollowable_chain: 2 pair(s)"  # one per chunk
    assert marked[-1] == "unfollowable_chain: 2 pair(s)"


def _uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _pair_row(name: str, value: str = "x", superseded_by: str = "", **kw) -> dict:
    """A row `_plan_chunk` reads, named by a short name its id is made from."""
    return {
        "id": _uid(name),
        "memory_key": "user_city",
        "value": value,
        "content_sha256": f"sha-{value}",
        "valid_at": None,
        "status": "superseded" if superseded_by else "active",
        "superseded_by": _uid(superseded_by) if superseded_by else "",
        "tombstoned_at": None,
        **kw,
    }


async def test_a_pair_that_no_longer_holds_is_not_followed() -> None:
    # Undone, or deleted, between the retirement and a deferred follow.
    from types import SimpleNamespace

    from server.memory.app.entity_projections import _plan_chunk

    class CH:
        def __init__(self, rows):
            self.rows, self.reads = rows, 0

        async def query_all(self, sql, params):
            self.reads += 1
            return self.rows if self.reads == 1 else []

    inp = SimpleNamespace(org="o", namespace="ns", agent="a")
    for loser in (_pair_row("o"), _pair_row("o", superseded_by="n", tombstoned_at="x")):
        ch = CH([loser, _pair_row("n")])
        plan, unread = await _plan_chunk(inp, [(_uid("o"), _uid("n"))], ch=ch)
        assert unread == []
        assert not plan.closes and not plan.inserts
        assert ch.reads == 1, "no triples read for a pair that no longer holds"


def test_a_chain_that_loops_or_was_not_read_has_no_head() -> None:
    from server.memory.app.entity_projections import _chain_head

    a, b, c = _pair_row("a", superseded_by="b"), _pair_row("b", superseded_by="a"), _pair_row("c")
    assert _chain_head(a, {a["id"]: a, b["id"]: b}) is None
    assert _chain_head(a, {a["id"]: a}) is None
    assert _chain_head(c, {c["id"]: c}) is c


class _Graph:
    """Rows by id, and the open (subject, relation, object) triples follows change;
    its triple reads filter as ClickHouse's do. `links` are `entity_triple_rows`:
    (row id, content hash, subject, relation, object, inferred). `fail` counts writes to fail
    once each, by table (or "close")."""

    def __init__(self, rows: list[dict], objects: set[str]) -> None:
        self.rows = {r["id"]: r for r in rows}
        self.open = {_triple(o) for o in objects}
        self.links: set[tuple[str, str, str, str, str, bool]] = set()
        self.closed_at: dict[str, datetime] = {}
        self.close_calls = 0
        self.inserted: list[tuple[str, str, str]] = []
        self.provenance: set[tuple[str, str, str, str]] = set()
        self.fail: dict[str, int] = {}
        # Run as the close is queued: a write that lands while the follow runs.
        self.during_close = lambda: None

    def _failing(self, what: str) -> None:
        if self.fail.get(what):
            self.fail[what] -= 1
            raise TimeoutError(what)

    def link(self, name: str, *objects: str) -> None:
        """Row `name`'s current value stated these objects' triples."""
        row = self.rows[_uid(name)]
        self.links |= {(row["id"], row["content_sha256"], *_triple(o), False) for o in objects}

    @staticmethod
    def _holds(row: dict) -> bool:
        # GRAPH_HOLDER_SQL over the fields these rows carry.
        return row["tombstoned_at"] is None and row["status"] in LIVE_STATUSES

    async def query_all(self, sql, params):
        if "pairs" in params:
            return []  # the relation's stored values: none beyond the open ones here
        if "names" in params or "now" in params:
            return []  # no stored intervals or later ends: open triples only
        if "SELECT DISTINCT subject, relation, object" in sql and "ids" in params:
            # The triples linked to rows, whatever value each holds now.
            return [
                {"subject": s, "relation": r, "object": o}
                for i, _h, s, r, o, _inf in sorted(self.links)
                if i in params["ids"]
            ]
        if "WITH keyed AS" in sql:
            # Live rows with no row link, of a key a triple is linked to.
            linked = {link[0] for link in self.links}
            return [
                {
                    "id": row["id"],
                    "sha": row["content_sha256"],
                    "updated_at": None,
                    "value": row["value"],
                    "subject": s,
                    "relation": r,
                    "object": o,
                }
                for row in self.rows.values()
                if self._holds(row) and row["id"] not in linked
                for s, r, o, key in sorted(self.provenance)
                if key == row["memory_key"] and (s, r, o) in params["triples"]
            ]
        if "WITH links AS" in sql:
            live = {r["id"]: r for r in self.rows.values() if self._holds(r)}
            return [
                {
                    "id": i,
                    "current": live[i]["content_sha256"],
                    "sha": h,
                    "value": live[i]["value"],
                    "updated_at": None,
                    "inferred": inf,
                    "subject": s,
                    "relation": r,
                    "object": o,
                }
                for i, h, s, r, o, inf in sorted(self.links)
                if (s, r, o) in params["triples"]
                and i in live
                and (inf or h == live[i]["content_sha256"])
            ]
        if "FROM loom.entity_triple_rows" in sql:
            keys, ids = set(params["keys"]), set(params.get("ids") or ())
            return [
                {"id": i, "sha": h, "inferred": inf, "subject": s, "relation": r, "object": o}
                for i, h, s, r, o, inf in sorted(self.links)
                if (not inf and (i, h) in keys) or (inf and i in ids)
            ]
        if "FROM loom.memory FINAL" in sql:
            assert all(i == str(uuid.UUID(i)) for i in params["ids"]), "ClickHouse's spelling"
            return [
                {**self.rows[i], "holds_facts": self._holds(self.rows[i])}
                for i in params["ids"]
                if i in self.rows
            ]
        if "triples" in params:
            hits = [t for t in self.open if t in params["triples"]]
        else:
            text = params["txt"].lower()
            hits = [
                t for t in self.open if (t[0] in text and t[2] in text) or t in params["linked"]
            ]
        return [{"subject": s, "relation": r, "object": o} for s, r, o in sorted(hits)]

    async def insert(self, table, batch, settings=None):
        self._failing(table)
        if table == "loom.entity_triples":
            self.inserted += [(r["subject"], r["relation"], r["object"]) for r in batch]
            self.open |= {(r["subject"], r["relation"], r["object"]) for r in batch}
        elif table == "loom.entity_triple_rows":
            self.links |= {
                (
                    r["memory_id"],
                    r["content_sha256"],
                    r["subject"],
                    r["relation"],
                    r["object"],
                    r["inferred"],
                )
                for r in batch
            }
        else:
            self.provenance |= {
                (r["subject"], r["relation"], r["object"], r["memory_key"]) for r in batch
            }

    async def close_triples(self, ch, *, org, namespace, closes):
        self._failing("close")
        self.close_calls += 1
        self.during_close()
        for s, r, o, valid_to, _ in closes:
            self.open.discard((s, r, o))
            self.closed_at[o] = valid_to

    async def reopen_triples(self, ch, *, org, namespace, closes, closed_at):
        self._failing("reopen")
        self.open |= {c[:3] for c in closes}

    def objects(self) -> set[str]:
        return {o for _s, _r, o in self.open}


_CITIES = {"paris", "london", "rome"}


def _triple(obj: str) -> tuple[str, str, str]:
    return ("user", "live_in" if obj in _CITIES else "work_at", obj)


async def _follow(monkeypatch, graph: _Graph, *batches: list[tuple[str, str]]):
    """Follow each batch in turn; the last batch's outcome."""
    from types import SimpleNamespace

    from server.memory.app import entity_projections

    monkeypatch.setattr(entity_projections, "close_triples", graph.close_triples)
    monkeypatch.setattr(entity_projections, "reopen_triples", graph.reopen_triples)
    inp = SimpleNamespace(org="o", namespace="ns", agent="a")
    outcome = None
    for batch in batches:
        pairs = [(_uid(loser), _uid(winner)) for loser, winner in batch]
        outcome = await entity_projections.follow_row_supersessions(inp, pairs, ch=graph)
    return outcome


def _chain(*values: str, **head: object) -> list[dict]:
    """Rows c0 -> c1 -> ... each retired by the next; `head` overrides the last."""
    names = [f"c{i}" for i in range(len(values))]
    rows = [
        _pair_row(names[i], values[i], superseded_by=names[i + 1]) for i in range(len(names) - 1)
    ]
    return [*rows, _pair_row(names[-1], values[-1], **head)]


_ORDERS = [
    [("c0", "c1"), ("c1", "c2")],
    [("c1", "c2"), ("c0", "c1")],
    [("c1", "c2"), ("c0", "c1"), ("c0", "c1")],
]


@pytest.mark.parametrize("per_pair_calls", [False, True])
@pytest.mark.parametrize("pairs", _ORDERS)
@pytest.mark.parametrize(
    ("values", "before", "after"),
    [
        # Each value replaced: only the live one is left, and none retired comes back.
        (
            ["User lives in Paris", "User lives in London", "User lives in Rome"],
            {"paris", "london"},
            {"rome"},
        ),
        # Elaborated: the city every row restates stays open.
        (
            [f"User lives in Paris and works at {w}" for w in ("Acme", "Beta", "Gamma")],
            {"paris", "acme", "beta"},
            {"paris", "gamma"},
        ),
        # Back to the first value: it is what holds now.
        (
            ["User lives in Paris", "User lives in London", "User lives in Paris"],
            {"paris", "london"},
            {"paris"},
        ),
    ],
)
async def test_a_chain_of_retirements_leaves_what_its_live_row_states(
    monkeypatch, values, before, after, pairs, per_pair_calls
) -> None:
    graph = _Graph(_chain(*values), before)
    await _follow(monkeypatch, graph, *([p] for p in pairs) if per_pair_calls else [pairs])
    assert graph.objects() == after


@pytest.mark.parametrize(
    "rows",
    [
        _chain(
            "User lives in Paris", "User lives in London", "User lives in Rome", tombstoned_at="x"
        ),
        # The end was erased: the row it was retired behind is no longer stored.
        _chain("User lives in Paris", "User lives in London", "User lives in Rome")[:2],
        # A restored snapshot can carry a superseded_by ClickHouse can't parse.
        *(
            [
                _pair_row("c0", "User lives in Paris", superseded_by="c1"),
                {
                    **_pair_row("c1", "User lives in London", superseded_by="c2"),
                    "superseded_by": bad,
                },
            ]
            for bad in (
                "junk",
                "{" + _uid("c2") + "}",
                "urn:uuid:" + _uid("c2"),
                "urn:" + _uid("c2").replace("-", ""),
            )
        ),
    ],
)
async def test_a_chain_ending_in_nothing_closes_and_writes_no_successor(monkeypatch, rows) -> None:
    graph = _Graph(rows, {"paris"})
    await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == set()


async def test_a_chain_past_the_hop_budget_is_left_open_and_logged(monkeypatch, caplog) -> None:
    from server.memory.app.entity_projections import _CHAIN_HOPS

    cities = [f"User lives in City{i}" for i in range(_CHAIN_HOPS + 3)]
    graph = _Graph(_chain(*cities), {"city0"})
    with caplog.at_level("WARNING"):
        outcome = await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == {"city0"} and "chain longer than was read" in caplog.text
    assert outcome.unfollowed == [(_uid("c0"), _uid("c1"))], "still owed, for the repair"
    graph = _Graph(_chain(*cities[: _CHAIN_HOPS + 2]), {"city0"})
    await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == {f"city{_CHAIN_HOPS + 1}"}, "the budget's last link is still read"


@pytest.mark.parametrize("reverse", [False, True])
async def test_pairs_in_different_chunks_close_a_triple_once_at_the_later_date(
    monkeypatch, reverse
) -> None:
    from server.memory.app.entity_projections import _FOLLOW_CHUNK

    # Paris is closed by the first pair and by one a chunk later; "near the river"
    # still stated it until 05-30.
    rows = [
        _pair_row("p0", "User lives in Paris", superseded_by="q0"),
        _pair_row("q0", "User lives in Berlin", valid_at=_d(23)),
        _pair_row("p1", "User lives in Paris near the river", superseded_by="q1"),
        _pair_row("q1", "User lives in Berlin", valid_at=_d(30)),
    ]
    pairs = [("p0", "q0")]
    for i in range(2, _FOLLOW_CHUNK + 1):
        rows += [_pair_row(f"p{i}", f"Note {i} old", superseded_by=f"q{i}"), _pair_row(f"q{i}")]
        pairs.append((f"p{i}", f"q{i}"))
    pairs.append(("p1", "q1"))
    graph = _Graph(rows, {"paris"})
    await _follow(monkeypatch, graph, pairs[::-1] if reverse else pairs)
    assert graph.closed_at == {"paris": _d(30)} and graph.close_calls == 1


async def test_a_triple_a_live_row_still_states_stays_open_beside_another_pairs_update(
    monkeypatch,
) -> None:
    # One key elaborates Paris; another key moved from Paris to Berlin.
    rows = [
        _pair_row("x", "User lives in Paris", superseded_by="y"),
        _pair_row("y", "User lives in Paris, near the river"),
        _pair_row("z", "User lives in Paris", superseded_by="w"),
        _pair_row("w", "User lives in Berlin"),
    ]
    graph = _Graph(rows, {"paris"})
    await _follow(monkeypatch, graph, [("x", "y"), ("z", "w")])
    assert graph.objects() == {"paris", "berlin"}


async def test_a_replay_after_a_failed_successor_insert_writes_it(monkeypatch) -> None:
    rows = [
        _pair_row("x", "User lives in Paris", superseded_by="y"),
        _pair_row("y", "User lives in Berlin"),
    ]
    graph = _Graph(rows, {"paris"})
    graph.fail["loom.entity_triples"] = 1
    with pytest.raises(TimeoutError):
        await _follow(monkeypatch, graph, [("x", "y")])
    assert graph.objects() == {"paris"}, "nothing closed before the successor is in"
    await _follow(monkeypatch, graph, [("x", "y")])
    assert graph.objects() == {"berlin"}


async def test_a_replay_after_a_failed_close_does_not_write_the_successor_twice(
    monkeypatch,
) -> None:
    # The successor `berlin germany` is not a substring of "Berlin, Germany", so the
    # replay's read of open triples does not see it.
    rows = [
        _pair_row("x", "User lives in Paris, France", superseded_by="y"),
        _pair_row("y", "User lives in Berlin, Germany"),
    ]
    graph = _Graph(rows, {"paris"})
    graph.fail["close"] = 1
    with pytest.raises(TimeoutError):
        await _follow(monkeypatch, graph, [("x", "y")])
    await _follow(monkeypatch, graph, [("x", "y")])
    assert graph.objects() == {"berlin germany"}
    assert graph.inserted == [("user", "live_in", "berlin germany")]


async def test_a_replay_after_a_failed_provenance_insert_links_the_successor(
    monkeypatch,
) -> None:
    rows = [
        _pair_row("x", "User lives in Paris", superseded_by="y"),
        _pair_row("y", "User lives in Berlin"),
    ]
    graph = _Graph(rows, {"paris"})
    graph.fail["loom.entity_triple_memories"] = 1
    with pytest.raises(TimeoutError):
        await _follow(monkeypatch, graph, [("x", "y")])
    await _follow(monkeypatch, graph, [("x", "y")])
    assert graph.provenance == {("user", "live_in", "berlin", "user_city")}


async def test_a_link_spelled_another_way_is_still_followed(monkeypatch) -> None:
    rows = _chain("User lives in Paris", "User lives in London", "User lives in Rome")
    rows[1]["superseded_by"] = rows[1]["superseded_by"].upper().replace("-", "")
    graph = _Graph(rows, {"paris"})
    await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == {"rome"}


async def test_a_cycle_of_retirements_is_left_open_and_logged(monkeypatch, caplog) -> None:
    rows = [
        _pair_row("c0", "User lives in Paris", superseded_by="c1"),
        _pair_row("c1", "User lives in London", superseded_by="c0"),
    ]
    graph = _Graph(rows, {"paris"})
    with caplog.at_level("WARNING"):
        await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == {"paris"} and "a cycle of retirements" in caplog.text


async def test_a_closed_triple_ends_when_its_own_row_was_replaced(monkeypatch) -> None:
    from server.core.datetimes import naive_utc_now

    # London is undated: Paris's triple ends when London was written, not on Rome's
    # date (overlapping London) nor when the follow runs.
    rows = _chain(
        "User lives in Paris", "User lives in London", "User lives in Rome", valid_at=_d(9)
    )
    rows[1]["updated_at"] = _d(2)
    graph = _Graph(rows, {"paris"})
    await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == {"rome"} and graph.closed_at["paris"] == _d(2)
    # An erased winner has no date: the triple ends when it is followed.
    before = naive_utc_now().replace(microsecond=0)  # a close stamps whole seconds
    graph = _Graph(rows[:1], {"paris"})
    await _follow(monkeypatch, graph, [("c0", "c1")])
    assert before <= graph.closed_at["paris"] <= naive_utc_now()


async def test_newest_wins_still_retires_every_older_same_key_row_unjudged(monkeypatch) -> None:
    from server.memory import store
    from server.memory.app.key_supersession import supersede_same_key_rows

    seen = _wire_judged(monkeypatch, {})
    victims: list = []

    async def supersede(org, v, *, ch=None, skip=None, **_kw):
        victims.extend(v)
        return len(v)

    monkeypatch.setattr(store, "supersede_duplicates", supersede)
    new = [_row("n1", _d(20))]
    n = await supersede_same_key_rows(
        org="o",
        namespace="ns",
        new_rows=new,
        ch=_CH([new[0], _row("o1", _d(23))], seen),
        policy="newest_wins",
    )
    assert n == 1 and seen["judged"] == []
    assert [(v["memory_id"], v["superseded_by"]) for v in victims] == [("o1", "n1")]


async def test_a_verdict_lands_on_its_rows_result_after_a_skipped_entry(monkeypatch) -> None:
    # The first entry was skipped (pinned, unchanged, ...): it has a result but no row,
    # so row 0's verdict belongs to result 1.
    from types import SimpleNamespace

    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.search.clock import StageClock

    async def verdicts(*args, **kwargs):
        return {0: SimpleNamespace(verdict="clear", peers=[])}

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", verdicts)
    state = _BulkWriteState()
    state.results = [
        _BulkSetResult(memory_key="user_pinned", version=0),
        _BulkSetResult(memory_key="user_city", version=1),
    ]
    state.memory_rows = [{**_row("n1", _d(30)), "memory_key": "user_city"}]
    state.row_results = [1]
    await bulk_set._bulk_verdicts(None, state, [], [], StageClock({}))
    assert [r.conflict_verdict for r in state.results] == ["", "clear"]
    # The rows that land are known now: their keys by id, for the settling steps.
    assert state.key_of == {"n1": "user_city"}


async def test_a_successor_the_follow_wrote_closes_when_its_row_retires(monkeypatch) -> None:
    # London is the follow's successor, linked to c1; c1's extractor stated only Acme.
    paris, london = (
        _pair_row("c0", "I live in Paris", superseded_by="c1"),
        _pair_row("c1", "I live in London"),
    )
    graph = _Graph([paris, london], {"paris", "acme"})
    graph.link("c0", "paris")
    graph.link("c1", "acme")
    await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == {"acme", "london"}

    graph.rows[london["id"]] = _pair_row("c1", "I live in London", superseded_by="c2")
    rome = _pair_row("c2", "I live in Rome")
    graph.rows[rome["id"]] = rome
    graph.link("c2", "acme")
    await _follow(monkeypatch, graph, [("c1", "c2")])
    assert graph.objects() == {"acme", "rome"}


async def test_a_winner_stored_before_links_still_closes_its_own_triples(monkeypatch) -> None:
    # c1 has no links: the successor the follow writes for it must not become its only one.
    c0 = _pair_row("c0", "user lives in paris near acme", superseded_by="c1")
    c1 = _pair_row("c1", "user lives in london near acme")
    graph = _Graph([c0, c1], {"paris", "acme"})
    graph.link("c0", "paris")
    await _follow(monkeypatch, graph, [("c0", "c1")])
    assert graph.objects() == {"acme", "london"}

    graph.rows[c1["id"]] = _pair_row("c1", c1["value"], superseded_by="c2")
    rome = _pair_row("c2", "user lives in rome")
    graph.rows[rome["id"]] = rome
    await _follow(monkeypatch, graph, [("c1", "c2")])
    assert graph.objects() == {"rome"}


async def test_a_winner_is_linked_to_a_successor_another_row_opened(monkeypatch) -> None:
    x0 = _pair_row("x0", "I live in Paris", superseded_by="x1")
    x1, y = _pair_row("x1", "I live in London"), _pair_row("y", "I live in London")
    graph = _Graph([x0, x1, y], {"paris", "london", "acme"})
    graph.link("x0", "paris")
    graph.link("x1", "acme")
    graph.link("y", "london")
    await _follow(monkeypatch, graph, [("x0", "x1")])
    # London was open already: x1 gets its key link and its row link, not a second London.
    assert ("user", "live_in", "london", "user_city") in graph.provenance
    assert graph.inserted == []

    # y retires; x1 still says London, so London stays open.
    graph.rows[y["id"]] = _pair_row("y", y["value"], superseded_by="z")
    z = _pair_row("z", "I live in Rome")
    graph.rows[z["id"]] = z
    await _follow(monkeypatch, graph, [("y", "z")])
    assert graph.objects() == {"acme", "london", "rome"}


async def test_a_row_that_states_a_triple_while_it_closes_opens_it_again(monkeypatch) -> None:
    rows = [
        _pair_row("a0", "I live in London", superseded_by="a1"),
        _pair_row("a1", "I live in Berlin"),
        _pair_row("s", "I live in Rome"),
    ]
    graph = _Graph(rows, {"london", "rome"})
    graph.link("a0", "london")
    graph.link("s", "rome")

    def back_to_london() -> None:
        graph.rows[_uid("s")] = _pair_row("s", "I live in London")
        graph.link("s", "london")

    graph.during_close = back_to_london
    outcome = await _follow(monkeypatch, graph, [("a0", "a1")])
    assert "london" in graph.objects()
    assert outcome.closed == 0


async def test_a_reopen_that_fails_is_left_for_the_repair(monkeypatch) -> None:
    from datetime import datetime

    from server.memory.app import entity_projections, projection_failures

    rows = [
        _pair_row("a0", "I live in London", superseded_by="a1"),
        _pair_row("a1", "I live in Berlin"),
        _pair_row("s", "I live in London"),
    ]
    graph = _Graph(rows, {"london"})
    graph.link("a0", "london")
    recorded: list[tuple[str, dict]] = []

    async def record(input, *, projection, payload, **kw):
        recorded.append((projection, payload))

    monkeypatch.setattr(entity_projections, "record_input_projection_failure", record)
    # The row says London only once the follow's plan is read.
    graph.during_close = lambda: graph.link("s", "london")
    graph.fail["reopen"] = 1
    await _follow(monkeypatch, graph, [("a0", "a1")])
    assert "london" not in graph.objects()
    ((projection, payload),) = recorded
    ((*triple, until),) = payload["closes"]
    assert (projection, triple) == ("triples_reopen", ["user", "live_in", "london"])
    assert datetime.fromisoformat(until) and datetime.fromisoformat(payload["closed_at"])

    resolved: list[list[str]] = []

    async def mark_resolved(*, org, failure_ids, ch):
        resolved.append(failure_ids)

    monkeypatch.setattr(projection_failures, "mark_projection_failures_resolved", mark_resolved)
    failure = projection_failures.ProjectionFailureRow(
        failure_id="f1",
        org="o",
        namespace="ns",
        agent="a",
        projection=projection,
        source_operation="triples.follow_rows",
        source_resource_id="triples.follow_rows:ns",
        memory_keys=[],
        error_type="TimeoutError",
        error_message="reopen",
        payload=payload,
    )
    out = await projection_failures.retry_projection_failure(failure, ch=graph)
    assert (out.retried, out.resolved, resolved) == (True, True, [["f1"]])
    assert "london" in graph.objects()


async def test_a_reopen_no_retry_can_read_is_abandoned(monkeypatch) -> None:
    from server.memory.app import projection_failures

    abandoned: list[str] = []

    async def mark_abandoned(*, org, failure_id, reason, ch):
        abandoned.append(reason)

    monkeypatch.setattr(projection_failures, "mark_projection_failure_abandoned", mark_abandoned)
    failure = projection_failures.ProjectionFailureRow(
        failure_id="f1",
        org="o",
        namespace="ns",
        agent="a",
        projection=projection_failures.TRIPLES_REOPEN,
        source_operation="triples.follow_rows",
        source_resource_id="triples.follow_rows:ns",
        memory_keys=[],
        error_type="TimeoutError",
        error_message="reopen",
        payload={"closes": [["user", "live_in", "london", "not a date"]]},
    )
    out = await projection_failures.retry_projection_failure(failure, ch=_Graph([], set()))
    assert (out.resolved, abandoned) == (False, ["malformed_payload"])


@pytest.mark.parametrize(
    "payload",
    [
        {"restored_at": "2026-10-15T09:00:00"},
        {"restore_id": "u1"},
        {"restore_id": "u1", "restored_at": "not a date"},
    ],
)
async def test_an_undo_replay_without_its_id_or_instant_is_abandoned_unread(
    monkeypatch, payload
) -> None:
    # '' is every other row's restore id, and the clock would date the Undo at the replay.
    from server.memory.app import graph_undo, projection_failures

    abandoned: list[str] = []
    called: list[str] = []

    async def mark_abandoned(*, org, failure_id, reason, ch):
        abandoned.append(reason)

    async def never(*a, **k):
        called.append("read")

    monkeypatch.setattr(projection_failures, "mark_projection_failure_abandoned", mark_abandoned)
    monkeypatch.setattr(graph_undo, "rows_still_holding", never)
    monkeypatch.setattr(graph_undo, "reopen_held_facts", never)
    failure = projection_failures.ProjectionFailureRow(
        failure_id="f1",
        org="o",
        namespace="ns",
        agent="a",
        projection=projection_failures.TRIPLES_RECONCILE,
        source_operation="memory.restore",
        source_resource_id="memory.restore:ns",
        memory_keys=[],
        error_type="RuntimeError",
        error_message="reconcile",
        payload={"memory_ids": ["m1"], "reopen": True, "content_sha256": {"m1": "0" * 64}}
        | payload,
    )
    out = await projection_failures.retry_projection_failure(failure, ch=_Graph([], set()))
    assert (out.resolved, abandoned, called) == (False, ["malformed_payload"], [])


async def test_a_shared_successor_is_written_once_with_each_winners_key(monkeypatch) -> None:
    rows = [
        _pair_row("a0", "user lives in Paris", superseded_by="a1"),
        _pair_row("a1", "user lives in London"),
        _pair_row("b0", "user home is Paris", superseded_by="b1", memory_key="home"),
        _pair_row("b1", "user home is London", memory_key="home"),
    ]
    graph = _Graph(rows, {"paris"})
    await _follow(monkeypatch, graph, [("a0", "a1"), ("b0", "b1")])
    london = ("user", "live_in", "london")
    assert graph.inserted == [london]
    assert {(*london, "user_city"), (*london, "home")} <= graph.provenance


async def test_the_repair_reads_a_chain_a_write_could_not(monkeypatch) -> None:
    from types import SimpleNamespace

    from server.memory.app import entity_projections

    cities = [f"User lives in City{i}" for i in range(70)]
    graph = _Graph(_chain(*cities), {"city0"})
    monkeypatch.setattr(entity_projections, "close_triples", graph.close_triples)
    monkeypatch.setattr(entity_projections, "reopen_triples", graph.reopen_triples)
    outcome = await entity_projections.follow_row_supersessions(
        SimpleNamespace(org="o", namespace="ns", agent="a"),
        [(_uid("c0"), _uid("c1"))],
        ch=graph,
        hops=entity_projections.REPAIR_CHAIN_HOPS,
    )
    assert outcome.unfollowed == [] and graph.objects() == {"city69"}


def _index(state) -> None:
    # The id -> key map `_bulk_verdicts` builds once the rows that land are known.
    state.key_of = {r["memory_id"]: r.get("memory_key", "") for r in state.memory_rows}


async def test_a_fallback_marker_closed_in_the_same_second_gets_a_marker_of_its_own(
    monkeypatch,
) -> None:
    # Paris is stated again after London's close, and that marker is closed again in
    # the second Paris first closed. The next check names its marker apart from the
    # first, rather than finding it written, and labels both as fallbacks, not Undos.
    from types import SimpleNamespace

    from server.memory.app import entity_projections, graph_undo

    closed = datetime(2026, 9, 1, 12, 0, 0)

    def paris(seen: datetime) -> dict:
        return {
            "subject": "user",
            "relation": "live_in",
            "object": "paris",
            "valid_from": None,
            "valid_to": closed,
            "invalid_at": closed,
            "seen_at": seen,
            "adapter_name": "itest",
            "restore_id": "",
        }

    stored = [paris(datetime(2026, 9, 1, 11, 0, 0))]
    restated: list[tuple[str, str]] = []

    async def reopen(input, ids, *, ch, restore_id, restored_at, adapter_name, **_kw):
        restated.append((restore_id, adapter_name))

    async def all_held_by_one_row(org, namespace, triples, *, ch, rows=None):
        return {t: {entity_projections.Holder("m1", "sha", closed)} for t in triples}

    monkeypatch.setattr(graph_undo, "reopen_held_facts", reopen)
    monkeypatch.setattr(entity_projections, "live_holders", all_held_by_one_row)
    inp = SimpleNamespace(org="o", namespace="ns", agent="a")
    await entity_projections._keep_one_current(inp, list(stored), ch=None)
    stored.append(paris(closed.replace(microsecond=5)))  # the marker, closed again
    await entity_projections._keep_one_current(inp, list(stored), ch=None)
    assert len({restore_id for restore_id, _ in restated}) == 2
    assert {adapter for _, adapter in restated} == {"row-fallback"}
