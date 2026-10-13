"""A memory key names one attribute; under `newest_wins` the
newest row for a key is the live one, whatever session wrote it, and under the
default policy the conflict judge decides whether two sessions' rows of one key
are one attribute."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.keys import strip_variant_suffixes
from server.memory.app.key_supersession import (
    judged_victims,
    same_key_peers,
    same_key_victims,
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


def _row(mid: str, valid_at: str, *, user: str = "-", pinned: bool = False) -> dict:
    return {**_live(mid, "user_ethnicity", f"s-{mid}", pinned=pinned), "user_id": user} | {
        "value": f"value of {mid}",
        "valid_at": valid_at,
        "created_at": "2026-10-13 08:00:00",
    }


def test_same_key_peers_are_the_same_users_other_live_rows_newest_first() -> None:
    new = [_row("n1", "2023-05-30")]
    live = [
        _row("n1", "2023-05-30"),  # the write's own row
        _row("o1", "2023-05-23"),
        _row("o2", "2023-05-27"),
        _row("o3", "2023-05-24", user="someone-else"),
        _row("o4", "2023-05-25", pinned=True),
        {**_row("o5", "2023-05-26"), "memory_key": "user_hometown"},
    ]
    peers = same_key_peers(new, live)
    assert [p["memory_id"] for p in peers["n1"]] == ["o2", "o1"]
    assert same_key_peers(new, [_row("n1", "2023-05-30")]) == {}


def test_a_contradiction_retires_the_row_older_in_valid_time() -> None:
    new = [_row("n1", "2023-05-30")]
    peers = {"n1": [_row("o1", "2023-05-23")]}
    victims = judged_victims(new, peers, {("n1", "o1"): "contradiction"})
    assert [(v["memory_id"], v["superseded_by"]) for v in victims] == [("o1", "n1")]
    # An old session ingested after the current one: the new row is the one that retires.
    late = [_row("n2", "2023-05-20")]
    victims = judged_victims(
        late, {"n2": [_row("o1", "2023-05-23")]}, {("n2", "o1"): "contradiction"}
    )
    assert [(v["memory_id"], v["superseded_by"], v["session_id"]) for v in victims] == [
        ("n2", "o1", "s-n2")
    ]
    # ...unless it is pinned.
    pinned = [_row("n3", "2023-05-20", pinned=True)]
    assert judged_victims(pinned, {"n3": peers["n1"]}, {("n3", "o1"): "contradiction"}) == []


def test_an_elaboration_retires_its_peer_and_other_labels_keep_both() -> None:
    new = [_row("n1", "2023-05-20")]
    peers = {"n1": [_row("o1", "2023-05-23")]}
    victims = judged_victims(new, peers, {("n1", "o1"): "elaboration"})
    assert [(v["memory_id"], v["superseded_by"]) for v in victims] == [("o1", "n1")]
    for label in ("restatement", "related", "unrelated"):
        assert judged_victims(new, peers, {("n1", "o1"): label}) == []
    assert judged_victims(new, peers, {}) == []


def _wire_judged(monkeypatch, labels: dict[str, str], *, judge_on: bool = True) -> dict:
    from server.memory import store
    from server.memory.app import conflicts
    from server.memory.app.conflict_judge import Judgement
    from server.settings import settings

    seen: dict = {"queries": 0, "judged": [], "victims": []}

    async def judge_peers(new_value, peers):
        seen["judged"].append((new_value, [p.memory_id for p in peers]))
        return Judgement(verdict="conflict", ran=True, labels=labels, stated=labels)

    async def supersede(org, victims, *, ch=None, skip=None):
        seen["victims"] = victims
        return len(victims)

    monkeypatch.setattr(settings, "memory_conflict_judge", judge_on)
    monkeypatch.setattr(conflicts, "judge_peers", judge_peers)
    monkeypatch.setattr(store, "supersede_duplicates", supersede)
    return seen


class _CH:
    def __init__(self, rows: list[dict], seen: dict) -> None:
        self.rows, self.seen = rows, seen

    async def query_all(self, sql: str, params: dict) -> list[dict]:
        self.seen["queries"] += 1
        return self.rows


def test_a_bulk_write_under_the_default_policy_retires_what_the_judge_calls_one_attribute(
    monkeypatch,
) -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    new = [_row("n1", "2023-05-30")]
    live = [new[0], _row("o1", "2023-05-23")]
    seen = _wire_judged(monkeypatch, {"o1": "contradiction"})

    def run(**kw) -> int:
        return asyncio.run(
            supersede_same_key_rows(org="o", namespace="ns", new_rows=new, ch=_CH(live, seen), **kw)
        )

    assert run(policy="auto_reinforce", judged=True) == 1
    assert seen["judged"] == [("value of n1", ["o1"])]
    assert [(v["memory_id"], v["superseded_by"]) for v in seen["victims"]] == [("o1", "n1")]
    # A point write, another policy, or the judge switched off leaves both rows live
    # without reading them.
    seen["queries"] = 0
    assert run(policy="auto_reinforce") == 0
    assert run(policy="manual_review", judged=True) == 0
    assert run(policy="append_only", judged=True) == 0
    _wire_judged(monkeypatch, {"o1": "contradiction"}, judge_on=False)
    assert run(policy="auto_reinforce", judged=True) == 0
    assert seen["queries"] == 0


def test_a_peer_the_judge_left_unlabelled_stays_live(monkeypatch) -> None:
    from server.memory.app.conflict_judge import Judgement
    from server.memory.app.key_supersession import supersede_same_key_rows

    new = [_row("n1", "2023-05-30")]
    live = [new[0], _row("o1", "2023-05-23"), _row("o2", "2023-05-25")]
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

    from server.memory.app import conflicts

    monkeypatch.setattr(conflicts, "judge_peers", judge_peers)
    n = asyncio.run(
        supersede_same_key_rows(
            org="o",
            namespace="ns",
            new_rows=new,
            ch=_CH(live, seen),
            policy="auto_reinforce",
            judged=True,
        )
    )
    assert n == 0 and seen["victims"] == []


def test_newest_wins_still_retires_every_older_same_key_row_unjudged(monkeypatch) -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    new = [_row("n1", "2023-05-20")]
    seen = _wire_judged(monkeypatch, {})
    n = asyncio.run(
        supersede_same_key_rows(
            org="o",
            namespace="ns",
            new_rows=new,
            ch=_CH([new[0], _row("o1", "2023-05-23")], seen),
            policy="newest_wins",
            judged=True,
        )
    )
    assert n == 1 and seen["judged"] == []
    assert [(v["memory_id"], v["superseded_by"]) for v in seen["victims"]] == [("o1", "n1")]
