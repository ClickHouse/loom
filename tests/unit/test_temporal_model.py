"""The interval semantics of bi-temporal assertions, against the issue's oracle and a
pointwise reference model."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from server.memory.domain import temporal_model as tm


def d(day: int, month: int = 1) -> datetime:
    return datetime(2026, month, day, tzinfo=UTC)


class Journal:
    """Commits in sequence order, reduced at a knowledge time."""

    def __init__(self) -> None:
        self.commits: list[tuple[datetime, tm.Amendment]] = []

    def commit(self, at: datetime, amendment: tm.Amendment) -> None:
        self.commits.append((at, amendment))

    def served(self, k: datetime | None = None) -> dict[str, tm.Assertion]:
        out: dict[str, tm.Assertion] = {}
        for at, a in self.commits:
            if k is not None and at > k:
                break
            out = tm.apply(out, a)
        return out

    def values(self, v: datetime, k: datetime | None = None) -> list[str]:
        return [a.value for a in tm.valid_now(self.served(k).values(), v)]


def oracle() -> Journal:
    j = Journal()
    j.commit(d(10), tm.assert_value([], tm.first("A", valid_from=d(1), validity_basis="stated")))
    served = list(j.served().values())
    j.commit(d(20), tm.correct(served, d(5), d(15), tm.first("B", validity_basis="stated")))
    return j


@pytest.mark.parametrize(
    ("v", "k", "want"),
    [
        (d(7), d(9), []),
        (d(7), d(12), ["A"]),
        (d(7), d(21), ["B"]),
        (d(3), d(21), ["A"]),
        (d(17), d(21), ["A"]),
    ],
)
def test_the_temporal_oracle(v: datetime, k: datetime, want: list[str]) -> None:
    assert oracle().values(v, k) == want


def test_a_correction_keeps_lineage_and_half_open_endpoints() -> None:
    j = oracle()
    served = j.served()
    assert sorted(a.value for a in served.values()) == ["A", "A", "B"]
    origins = {a.origin_assertion_id for a in served.values()}
    assert len(origins) == 1
    assert j.values(d(5)) == ["B"]
    assert j.values(d(15)) == ["A"]
    assert j.values(datetime(2026, 1, 14, 23, 59, tzinfo=UTC)) == ["B"]


def test_employer_change_is_a_transition_and_a_mistake_is_a_correction() -> None:
    # U01: a transition ends the old state; a correction rewrites the belief.
    moved = Journal()
    moved.commit(d(1), tm.assert_value([], tm.first("Acme", valid_from=d(1, 1))))
    moved.commit(d(2), tm.transition(list(moved.served().values()), d(1, 6), tm.first("Globex")))
    assert moved.values(d(1, 3)) == ["Acme"]
    assert moved.values(d(1, 7)) == ["Globex"]

    fixed = Journal()
    fixed.commit(d(1), tm.assert_value([], tm.first("Acme", valid_from=d(1, 1))))
    fixed.commit(d(2), tm.correct(list(fixed.served().values()), d(1, 1), None, tm.first("Globex")))
    assert fixed.values(d(1, 3)) == ["Globex"]
    assert fixed.values(d(1, 3), k=d(1)) == ["Acme"]


def test_an_older_conversation_fills_the_past_without_reverting_the_present() -> None:
    # U02: evidence about Jan arrives, as a plain dated write, after the Mar state.
    j = Journal()
    j.commit(d(1, 4), tm.assert_value([], tm.first("lives in Oslo", valid_from=d(1, 3))))
    late = tm.first("lives in Bergen", valid_from=d(1, 1))
    j.commit(d(2, 4), tm.assert_value(list(j.served().values()), late))
    assert j.values(d(10, 3)) == ["lives in Oslo"]
    assert j.values(d(10, 1)) == ["lives in Bergen"]
    assert j.values(d(10, 3), k=d(1, 4)) == ["lives in Oslo"]
    # Earlier still: it fills up to Bergen and keeps what was recorded before it.
    j.commit(
        d(3, 4),
        tm.assert_value(
            list(j.served().values()),
            tm.first("in Rome", valid_from=datetime(2025, 12, 1, tzinfo=UTC)),
        ),
    )
    j.commit(
        d(4, 4),
        tm.assert_value(
            list(j.served().values()),
            tm.first("in Pisa", valid_from=datetime(2025, 12, 15, tzinfo=UTC)),
        ),
    )
    assert [j.values(datetime(2025, 12, day, tzinfo=UTC)) for day in (5, 20)] + [
        j.values(d(10, 1))
    ] == [
        ["in Rome"],
        ["in Pisa"],
        ["lives in Bergen"],
    ]


def test_a_cancelled_future_policy_keeps_the_earlier_expectation() -> None:
    # U03: a future-effective transition, then cancelled.
    j = Journal()
    j.commit(d(1), tm.assert_value([], tm.first("plan A", valid_from=d(1))))
    j.commit(d(2), tm.transition(list(j.served().values()), d(1, 6), tm.first("plan B")))
    future = [a for a in j.served().values() if a.value == "plan B"]
    j.commit(d(3), tm.retract(list(j.served().values()), [a.assertion_id for a in future]))
    assert j.values(d(1, 7), k=d(2)) == ["plan B"]
    assert j.values(d(1, 7)) == []
    assert j.values(d(1, 3)) == ["plan A"]


def test_a_repeated_value_is_a_separate_episode() -> None:
    # U04: A -> B -> A keeps three episodes, not one.
    j = Journal()
    j.commit(d(1), tm.assert_value([], tm.first("tea", valid_from=d(1, 1))))
    j.commit(d(2), tm.transition(list(j.served().values()), d(1, 3), tm.first("coffee")))
    j.commit(d(3), tm.transition(list(j.served().values()), d(1, 5), tm.first("tea")))
    episodes = sorted(j.served().values(), key=lambda a: a.valid_from or d(1))
    assert [a.value for a in episodes] == ["tea", "coffee", "tea"]
    assert episodes[0].assertion_id != episodes[2].assertion_id


def test_a_temporary_override_does_not_invent_a_resumption() -> None:
    # U05: an override over [Mar, Apr) on an open state ends there; with no
    # continuity policy the state after it is the supported original segment only
    # when one existed. Here the original was closed, so nothing resumes.
    j = Journal()
    j.commit(d(1), tm.assert_value([], tm.first("office", valid_from=d(1, 1), valid_to=d(1, 3))))
    j.commit(d(2), tm.correct(list(j.served().values()), d(1, 3), d(1, 4), tm.first("remote")))
    assert j.values(d(15, 3)) == ["remote"]
    assert j.values(d(15, 4)) == []


def test_disagreeing_sources_are_a_dispute_not_last_write_wins() -> None:
    # U06: two sources over the same period both stay served.
    j = Journal()
    a = tm.first("price 10", valid_from=d(1))
    b = tm.first("price 12", valid_from=d(1))
    j.commit(d(2), tm.Amendment("assert", (), (a, b)))
    held = tm.valid_now(j.served().values(), d(5))
    assert tm.disputed(held)
    # Different qualifiers are not a dispute (U16).
    c = tm.first("role: author", qualifiers={"project": "x"})
    e = tm.first("role: reviewer", qualifiers={"project": "y"})
    assert not tm.disputed([c, e])


def test_events_and_snapshots_are_not_running_states() -> None:
    # U18: a payment event occurred; a balance snapshot is superseded by the next one.
    paid = tm.first("paid invoice 7", temporal_kind="event", valid_from=d(3))
    assert not paid.valid_at(d(2))
    assert paid.valid_at(d(9))
    s1 = tm.first("balance 5", temporal_kind="snapshot", valid_from=d(3))
    s2 = tm.first("balance 9", temporal_kind="snapshot", valid_from=d(8))
    assert [a.value for a in tm.valid_now([s1, s2], d(5))] == ["balance 5"]
    assert [a.value for a in tm.valid_now([s1, s2], d(9))] == ["balance 9"]
    # A correction over an event retires it whole; it never splits it.
    amend = tm.correct([paid], d(1), d(5), None)
    assert amend.retired == (paid.assertion_id,)
    assert amend.added == ()


def test_unknown_validity_is_excluded_under_strict_policy() -> None:
    # U09 / U25: an unknown bound is disclosed, not treated as an asserted open past.
    a = tm.first("x")
    assert a.valid_at(d(1))
    assert not a.valid_at(d(1), policy="strict")
    b = tm.first("y", valid_from=d(1), validity_basis="stated")
    assert b.valid_at(d(2), policy="strict")


def test_an_assessment_keeps_content_and_interval() -> None:
    # U14: a verification is a new assessment, visible from its commit.
    j = Journal()
    j.commit(d(1), tm.assert_value([], tm.first("A", valid_from=d(1))))
    j.commit(d(2), tm.assess(list(j.served().values()), verified_by="human:ana"))
    (now,) = j.served().values()
    (then,) = j.served(d(1)).values()
    assert now.verified_by == "human:ana" and then.verified_by == ""
    assert now.value == then.value and now.valid_from == then.valid_from
    assert tm.assess([now], verified_by="human:ana").empty


def test_a_restatement_is_empty_and_retries_hash_alike() -> None:
    a = tm.first("A", valid_from=d(1))
    assert tm.assert_value([a], tm.first("A", valid_from=d(1))).empty
    one = tm.assert_value([a], tm.first("B"))
    two = tm.assert_value([a], tm.first("B"))
    assert one.payload_sha256() == two.payload_sha256()
    assert one.payload_sha256() != tm.assert_value([a], tm.first("C")).payload_sha256()


def test_a_stale_amendment_is_refused() -> None:
    a = tm.first("A")
    with pytest.raises(ValueError, match="not served"):
        tm.apply({}, tm.retract([a]))


def test_json_round_trip() -> None:
    a = tm.first(
        "A",
        valid_from=d(1),
        valid_text="early January",
        valid_precision="month",
        tags=("t",),
        qualifiers={"role": "x"},
    )
    assert tm.Assertion.from_json(a.to_json()) == a


def _reference_point(history: list[tuple[str, int, int, str | None]], day: int) -> str | None:
    """Pointwise model: the last operation covering `day` decides its value."""
    value: str | None = None
    for op, lo, hi, v in history:
        if lo <= day < hi:
            value = v if op != "retract" else None
    return value


def test_random_corrections_and_transitions_match_the_pointwise_model() -> None:
    rng = random.Random(25)
    for _ in range(200):
        j = Journal()
        history: list[tuple[str, int, int, str | None]] = []
        base = d(1)
        j.commit(base, tm.assert_value([], tm.first("v0", valid_from=d(1), valid_to=d(29))))
        history.append(("set", 1, 29, "v0"))
        for step in range(1, 8):
            lo = rng.randint(1, 27)
            hi = rng.randint(lo + 1, 29)
            served = list(j.served().values())
            at = base + timedelta(hours=step)
            val = rng.choice(["a", "b", "c"])  # repeats reach the same-claim paths
            r = rng.random()
            if r < 0.2:
                j.commit(at, tm.correct(served, d(lo), d(hi), None))
                history.append(("retract", lo, hi, None))
            elif r < 0.45:
                # A transition holds from its day on, past every later plan.
                j.commit(at, tm.transition(served, d(lo), tm.first(val)))
                history.append(("set", lo, 29, val))
            else:
                j.commit(at, tm.correct(served, d(lo), d(hi), tm.first(val)))
                history.append(("set", lo, hi, val))
        for day in range(1, 29):
            got = j.values(d(day))
            want = _reference_point(history, day)
            assert got == ([want] if want else []), (history, day, got)
            assert len(got) <= 1


def test_claims_under_other_qualifiers_are_never_superseded() -> None:
    home = tm.first('"French"', qualifiers={"role": "home"})
    work = tm.first('"English"', qualifiers={"role": "work"})
    for change in (
        tm.correct([home], d(1), None, work),
        tm.transition([home], d(1), work),
        tm.assert_value([home], work),
    ):
        assert change.retired == ()
        assert [a.value for a in change.added] == ['"English"']


def test_a_transition_declares_its_bound() -> None:
    a = tm.first('"Acme"', valid_from=d(1), validity_basis="stated")
    change = tm.transition([a], d(10), tm.first('"Globex"'))
    new = change.added[-1]
    assert new.valid_from == d(10) and new.validity_basis == "stated"
    assert new.valid_at(d(11), policy="strict")


def test_a_snapshot_under_other_qualifiers_is_another_balance() -> None:
    home = tm.first("100", temporal_kind="snapshot", valid_from=d(1), qualifiers={"acct": "home"})
    work = tm.first("7", temporal_kind="snapshot", valid_from=d(2), qualifiers={"acct": "work"})
    later = tm.first("120", temporal_kind="snapshot", valid_from=d(3), qualifiers={"acct": "home"})
    assert {a.value for a in tm.valid_now([home, work], d(5))} == {"100", "7"}
    assert {a.value for a in tm.valid_now([home, work, later], d(5))} == {"120", "7"}


def test_an_earlier_date_for_the_same_value_fills_the_earlier_span() -> None:
    j = Journal()
    j.commit(d(1, 4), tm.assert_value([], tm.first("in Oslo", valid_from=d(1, 3))))
    j.commit(
        d(2, 4), tm.assert_value(list(j.served().values()), tm.first("in Oslo", valid_from=d(1, 1)))
    )
    assert j.values(d(10, 2)) == ["in Oslo"] and j.values(d(10, 4)) == ["in Oslo"]


def test_a_claim_already_served_still_closes_what_overlaps_it() -> None:
    # The span already holds exactly "b", yet another "b" from d7 overlaps it.
    held = tm.first("b", valid_to=d(9), validity_basis="stated")
    late = tm.first("b", valid_from=d(7), validity_basis="stated")
    j = Journal()
    j.commit(d(1), tm.Amendment("assert", (), (held, late)))
    j.commit(
        d(2),
        tm.correct(list(j.served().values()), None, d(9), tm.first("b", validity_basis="stated")),
    )
    assert j.values(d(7)) == ["b"]
    # A transition to a value the key already serves from earlier still ends the others.
    c = tm.first("c", valid_from=d(3), validity_basis="stated")
    b = tm.first("b", valid_from=d(1), validity_basis="stated")
    j = Journal()
    j.commit(d(1), tm.Amendment("assert", (), (c, b)))
    j.commit(d(2), tm.transition(list(j.served().values()), d(1), tm.first("b")))
    assert j.values(d(4)) == ["b"]


def test_a_transition_ends_a_served_rule() -> None:
    rule = tm.first("weekly", temporal_kind="rule", valid_from=d(1), validity_basis="stated")
    j = Journal()
    j.commit(d(1), tm.assert_value([], rule))
    j.commit(d(2), tm.transition([rule], d(10), tm.first("daily", temporal_kind="rule")))
    assert j.values(d(5)) == ["weekly"] and j.values(d(12)) == ["daily"]


def test_a_proposed_snapshot_never_hides_the_served_one() -> None:
    from server.memory.domain.temporal_reads import answer

    served = tm.first("100", temporal_kind="snapshot", valid_from=d(1), validity_basis="stated")
    proposed = tm.first(
        "200", temporal_kind="snapshot", valid_from=d(5), validity_basis="stated", status="proposed"
    )
    held, _ = answer([served, proposed], d(10))
    assert [a.value for a in held] == ["100"]


def test_plain_writes_keep_history_they_do_not_touch() -> None:
    # An undated restatement after a transition keeps the past (it was retiring it).
    j = Journal()
    j.commit(d(1), tm.assert_value([], tm.first("a", valid_from=d(1), validity_basis="stated")))
    j.commit(d(2), tm.transition(list(j.served().values()), d(5), tm.first("b")))
    j.commit(d(6), tm.assert_value(list(j.served().values()), tm.first("b")))
    assert j.values(d(3)) == ["a"] and j.values(d(7)) == ["b"]
    # A closed past write over an open-ended state corrects that span only.
    j = Journal()
    j.commit(
        d(1), tm.assert_value([], tm.first("Berlin", valid_from=d(1), validity_basis="stated"))
    )
    paris = tm.first("Paris", valid_from=d(10), valid_to=d(20), validity_basis="stated")
    j.commit(d(21), tm.assert_value(list(j.served().values()), paris))
    assert [j.values(d(day)) for day in (5, 15, 25)] == [["Berlin"], ["Paris"], ["Berlin"]]


def test_a_closed_dated_write_over_an_undated_present_fills_only_its_span() -> None:
    berlin = tm.first("Berlin")
    paris = tm.first("Paris", valid_from=d(10), valid_to=d(20), validity_basis="stated")
    served = tm.apply({berlin.assertion_id: berlin}, tm.assert_value([berlin], paris))
    at = lambda v: sorted(a.value for a in tm.valid_now(served.values(), v))  # noqa: E731
    assert (at(d(5)), at(d(15)), at(d(25))) == (["Berlin"], ["Paris"], ["Berlin"])


def test_same_value_extension_is_idempotent_and_never_unbounded() -> None:
    b = tm.first("b", valid_from=d(9), valid_to=d(11), validity_basis="stated")
    once = tm.apply(
        {b.assertion_id: b},
        tm.assert_value([b], tm.first("b", valid_from=d(6), validity_basis="stated")),
    )
    again = tm.assert_value(
        list(once.values()), tm.first("b", valid_from=d(6), validity_basis="stated")
    )
    assert again.empty
    # A closed write that does not reach the later same-value state stays closed.
    a_open = tm.first("a", valid_from=d(10), validity_basis="stated")
    short = tm.first("a", valid_from=d(2), valid_to=d(3), validity_basis="stated")
    served = tm.apply({a_open.assertion_id: a_open}, tm.assert_value([a_open], short))
    assert sorted((x.valid_from, x.valid_to) for x in served.values()) == [
        (d(2), d(3)),
        (d(10), None),
    ]


def test_plain_writes_never_invert_an_interval_or_pass_a_plan() -> None:
    now = d(15, 2)
    j = Journal()
    j.commit(d(1), tm.assert_value([], tm.first("alpha"), now=now))
    j.commit(d(2), tm.transition(list(j.served().values()), d(1, 3), tm.first("bravo")))
    j.commit(
        d(3),
        tm.assert_value(
            list(j.served().values()),
            tm.first("charlie", valid_from=d(1, 6), validity_basis="stated"),
            now=now,
        ),
    )
    assert all(
        a.valid_to is None or a.valid_from is None or a.valid_from < a.valid_to
        for a in j.served().values()
    )
    assert (
        j.values(d(1, 1)) == ["alpha"]
        and j.values(d(1, 4)) == ["bravo"]
        and j.values(d(1, 7)) == ["charlie"]
    )
    # An undated write revises what holds now and stops at a plan.
    j = Journal()
    j.commit(
        d(1),
        tm.assert_value(
            [], tm.first("alpha", valid_from=d(1), valid_to=d(1, 12), validity_basis="stated")
        ),
    )
    j.commit(d(2), tm.transition(list(j.served().values()), d(1, 8), tm.first("plan")))
    j.commit(d(3), tm.assert_value(list(j.served().values()), tm.first("charlie"), now=now))
    assert j.values(now) == ["charlie"] and j.values(d(1, 9)) == ["plan"]


def test_a_narrower_span_of_a_value_held_and_a_new_snapshot_add_nothing_false() -> None:
    xray = tm.first("xray", valid_from=d(1), validity_basis="stated")
    assert tm.assert_value(
        [xray], tm.first("xray", valid_from=d(1, 3), valid_to=d(1, 4), validity_basis="stated")
    ).empty
    s100 = tm.first("100", temporal_kind="snapshot", valid_from=d(1), validity_basis="stated")
    s200 = tm.first("200", temporal_kind="snapshot", valid_from=d(1, 5), validity_basis="stated")
    am = tm.assert_value([s100], s200)
    assert am.retired == () and [a.value for a in am.added] == ["200"]


def test_a_dated_rule_restated_as_a_state_over_its_span_replaces_it() -> None:
    rule = tm.first("weekly", temporal_kind="rule", valid_from=d(1), validity_basis="stated")
    state = tm.first("daily", valid_from=d(1), validity_basis="stated")
    served = tm.apply({rule.assertion_id: rule}, tm.assert_value([rule], state))
    assert [a.value for a in tm.valid_now(served.values(), d(5))] == ["daily"]


def test_a_dated_restatement_with_new_metadata_assesses_the_covering_state() -> None:
    hv = tm.first("hv", valid_from=d(1))
    am = tm.assert_value([hv], tm.first("hv", valid_from=d(1, 2), sensitivity="high", tags=("x",)))
    assert am.change_kind == "assess" and am.retired == (hv.assertion_id,)
    (b,) = am.added
    assert (b.valid_from, b.sensitivity, b.tags) == (d(1), "high", ("x",))
    assert tm.assert_value([hv], tm.first("hv", valid_from=d(1, 2))).empty


def test_a_dated_write_on_a_rule_key_keeps_the_rule_outside_its_span() -> None:
    rule1 = tm.first("r1", temporal_kind="rule", valid_from=d(1))
    am = tm.assert_value(
        [rule1], tm.first("r2", temporal_kind="rule", valid_from=d(1, 3), valid_to=d(1, 4))
    )
    after = tm.apply({rule1.assertion_id: rule1}, am).values()
    assert [a.value for a in tm.valid_now(after, d(1, 2))] == ["r1"]
    assert [a.value for a in tm.valid_now(after, d(15, 3))] == ["r2"]
    assert [a.value for a in tm.valid_now(after, d(1, 5))] == ["r1"]


def test_an_undated_end_never_passes_a_plan_or_precedes_its_start() -> None:
    now = d(1, 6)
    cur = tm.first("cur", valid_from=d(1, 1))
    plan = tm.first("plan", valid_from=d(1, 9))
    (b,) = tm.assert_value([cur, plan], tm.first("cur2", valid_to=d(1, 12)), now=now).added
    assert (b.valid_from, b.valid_to) == (d(1, 1), d(1, 9))
    with pytest.raises(tm.EmptyInterval):
        tm.assert_value([cur], tm.first("e2", valid_to=d(1, 1)), now=now)
    with pytest.raises(tm.EmptyInterval):
        tm.assert_value([], tm.first("x", valid_from=d(1, 5), valid_to=d(1, 3)), now=now)


def test_of_events_the_current_row_shows_the_latest_to_have_happened() -> None:
    new = tm.first("new", temporal_kind="event", valid_from=d(1, 5))
    old = tm.first("old", temporal_kind="event", valid_from=d(1, 1))
    assert [a.value for a in tm.current_claims([new, old], d(1, 10))] == ["new"]


def test_a_same_value_write_with_new_temporal_metadata_is_not_a_restatement() -> None:
    rule = tm.first("weekly", temporal_kind="rule", valid_from=d(1))
    am = tm.assert_value([rule], tm.first("weekly", valid_from=d(1), modality="planned"))
    assert not am.empty
    (b,) = am.added
    assert (b.temporal_kind, b.modality) == ("state", "planned")
