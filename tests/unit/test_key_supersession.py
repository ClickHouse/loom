"""A memory key names one attribute; under `newest_wins` the
newest row for a key is the live one, whatever session wrote it, and under the
default policy the conflict judge decides whether two sessions' rows of one key
are one attribute."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

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
    }


def _d(day: int) -> datetime:
    return datetime(2023, 5, day)


def _pairs(out) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    return out.new_rows, [(v["memory_id"], v["superseded_by"]) for v in out.peers]


def test_same_key_peers_are_the_same_subjects_other_live_rows_newest_first() -> None:
    new = [_row("n1", _d(30))]
    live = [
        _row("n1", _d(30)),  # the write's own row
        _row("o1", _d(23)),
        _row("o2", _d(27)),
        _row("o3", _d(24), user="someone-else"),
        _row("o4", _d(25), pinned=True),
        {**_row("o5", _d(26)), "memory_key": "user_hometown"},
        _row("o6", _d(28), agent="another-agent"),  # anonymous: another writer's subject
    ]
    peers = same_key_peers(new, live)
    assert [p["memory_id"] for p in peers["n1"]] == ["o2", "o1"]
    assert same_key_peers(new, [_row("n1", _d(30))]) == {}
    # A named user is one subject whichever agent wrote the row.
    named = [_row("n1", _d(30), user="u")]
    assert [
        p["memory_id"]
        for p in same_key_peers(named, [_row("o1", _d(23), user="u", agent="b")])["n1"]
    ] == ["o1"]


def test_a_new_row_the_batch_already_retired_or_marked_duplicate_is_not_judged() -> None:
    live = [_row("o1", _d(23))]
    for status in ("superseded", "duplicate"):
        assert same_key_peers([_row("n1", _d(30), status=status)], live) == {}
    assert same_key_peers([_row("n1", _d(30), status="conflicted")], live)


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


def test_two_new_rows_one_retiring_behind_a_peer_the_other_retires() -> None:
    # n_old (05-20) loses to o1 (05-23); n_new (05-30) retires o1. n_old then points at
    # n_new, not at the dead o1.
    n_old, n_new = _row("n_old", _d(20)), _row("n_new", _d(30))
    o1 = _row("o1", _d(23))
    labels = {("n_old", "o1"): "contradiction", ("n_new", "o1"): "contradiction"}
    out = judged_victims([n_old, n_new], {"n_old": [o1], "n_new": [o1]}, labels)
    assert _pairs(out) == ([("n_old", "n_new")], [("o1", "n_new")])


def test_a_retired_new_row_lands_superseded_on_both_rows() -> None:
    rows = [_row("n1", _d(20)), {**_row("n2", _d(21)), "memory_key": "user_hometown"}]
    embs = [
        {
            "memory_key": r["memory_key"],
            "agent": r["agent"],
            "user_id": r["user_id"],
            "session_id": r["session_id"],
            "status": "active",
            "invalid_at": None,
        }
        for r in rows
    ]
    now = datetime(2026, 10, 13, 9, 0)
    stamp_new_row_retirements(rows, embs, [("n1", "o1")], {"o1": _row("o1", _d(23))}, now)
    assert (rows[0]["status"], rows[0]["superseded_by"], rows[0]["invalid_at"]) == (
        "superseded",
        "o1",
        _d(23),
    )
    assert (embs[0]["status"], embs[0]["invalid_at"]) == ("superseded", _d(23))
    # The other row keeps its state but carries the same columns.
    assert (rows[1]["status"], rows[1]["superseded_by"], rows[1]["invalid_at"]) == (
        "active",
        "",
        None,
    )
    assert embs[1]["status"] == "active"
    # A winner without valid time: `now`.
    rows = [_row("n1", _d(20))]
    stamp_new_row_retirements(rows, [], [("n1", "o1")], {"o1": _row("o1", None)}, now)
    assert rows[0]["invalid_at"] == now


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


class _CH:
    def __init__(self, rows: list[dict], seen: dict) -> None:
        self.rows, self.seen = rows, seen

    async def query_all(self, sql: str, params: dict) -> list[dict]:
        self.seen["queries"].append(params)
        self.seen["sql"] = sql
        return self.rows


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
    assert params["keys"] == ["user_ethnicity"] and params["users"] == ["u"]
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
    state = _BulkWriteState()
    state.memory_rows = [
        _row("n1", _d(20), status="conflicted"),
        {**_row("n2", _d(21)), "memory_key": "user_hometown"},
    ]
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
    assert state.superseded_on_write == ["user_ethnicity"]
    assert state.memory_rows[0]["status"] == "superseded"


async def test_bulk_retires_judged_peers_after_the_insert_and_links_only_live_rows(
    monkeypatch,
) -> None:
    from server.memory.app import bulk_set, key_supersession, value_updates
    from server.memory.app.bulk_rows import _BulkWriteState
    from server.memory.app.key_supersession import JudgedRetirements, _victim

    calls: dict = {}

    async def no_same_key(**kw):
        return 0

    async def retire(org, victims, *, ch):
        calls["retired"] = [v["memory_id"] for v in victims]
        return len(victims)

    async def link(**kw):
        calls["linked"] = [r["memory_id"] for r in kw["rows"]]
        return type("R", (), {"links": []})()

    monkeypatch.setattr(key_supersession, "supersede_same_key_rows", no_same_key)
    monkeypatch.setattr(key_supersession, "retire_judged_peers", retire)
    monkeypatch.setattr(value_updates, "link_value_updates", link)
    state = _BulkWriteState()
    state.memory_rows = [_row("n1", _d(30)), {**_row("n2", _d(20)), "status": "superseded"}]

    class Input:
        org, namespace, detect_updates = "o", "ns", True

    judged = JudgedRetirements(peers=[_victim(_row("o1", _d(23)), "n1")])
    await bulk_set._bulk_post_insert_links(None, Input(), state, judged)
    assert calls == {"retired": ["o1"], "linked": ["n1"]}


async def test_newest_wins_still_retires_every_older_same_key_row_unjudged(monkeypatch) -> None:
    from server.memory import store
    from server.memory.app.key_supersession import supersede_same_key_rows

    seen = _wire_judged(monkeypatch, {})
    victims: list = []

    async def supersede(org, v, *, ch=None, skip=None):
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
