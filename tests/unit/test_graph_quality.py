from datetime import datetime, timedelta

import pytest

from server.memory.domain.graph_quality import (
    canonical_edge_pair,
    canonical_entity,
    canonical_entity_text,
    canonical_relation,
    is_single_valued_relation,
    triple_interval_repairs,
)


def test_canonical_entity_strips_articles_possessives_and_punctuation() -> None:
    entity = canonical_entity("The Vatican's Deli!")

    assert entity.text == "vatican deli"
    assert entity.entity_id == "vatican_deli"
    assert entity.aliases == ["The Vatican's Deli!"]
    assert canonical_entity_text("  an Acme, Inc.  ") == "acme inc"


def test_canonical_relation_marks_out_of_ontology_lower_confidence() -> None:
    accepted = canonical_relation("Works At", accepted_verbs={"works_at"})
    rejected = canonical_relation("mentions", accepted_verbs={"works_at"})

    assert accepted.verb == "works_at"
    assert accepted.accepted is True
    assert accepted.confidence == 1.0
    assert rejected.verb == "mentions"
    assert rejected.accepted is False
    assert rejected.confidence == 0.5


def test_triple_interval_repairs_close_conflicting_current_state() -> None:
    repairs = triple_interval_repairs(
        existing=[
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2023, 1, 1),
                "valid_to": None,
            }
        ],
        new_subject="User",
        new_relation="works at",
        new_object="ClickHouse",
        new_valid_at=datetime(2025, 1, 1),
    )

    assert len(repairs) == 1
    assert repairs[0].old_object == "acme"
    assert repairs[0].new_object == "clickhouse"
    assert repairs[0].valid_to == datetime(2025, 1, 1)
    assert repairs[0].valid_from == datetime(2023, 1, 1)
    assert canonical_edge_pair(repairs[0].subject, repairs[0].old_object) == (
        "acme",
        "user",
    )


def test_a_value_stated_with_its_own_end_is_not_closed_early() -> None:
    # A documented limit: "works at Acme until December" stays current until then.
    repairs = triple_interval_repairs(
        existing=[
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2023, 1, 1),
                "valid_to": datetime(2025, 12, 1),
            }
        ],
        new_subject="user",
        new_relation="works at",
        new_object="ClickHouse",
        new_valid_at=datetime(2025, 1, 1),
    )

    assert repairs == []


def test_triple_interval_repairs_do_not_close_multi_valued_relations() -> None:
    repairs = triple_interval_repairs(
        existing=[
            {
                "subject": "user",
                "relation": "owns",
                "object": "bike",
                "valid_from": datetime(2023, 1, 1),
                "valid_to": None,
            }
        ],
        new_subject="user",
        new_relation="owns",
        new_object="car",
        new_valid_at=datetime(2025, 1, 1),
    )

    assert repairs == []
    assert is_single_valued_relation("works at") is True
    assert is_single_valued_relation("owns") is False


# ---------------------------------------------------------------------------
# Cardinality is declared; the seed list is a default.
# ---------------------------------------------------------------------------


def test_declared_cardinality_folds_surface_forms_into_lemma_space() -> None:
    from server.memory.domain.graph_quality import declared_cardinality, is_single_valued_relation

    declared = declared_cardinality(
        [
            ("works_at", "one"),
            ("addresses", "many"),
            ("mentors", "ONE"),
            ("likes", ""),
            ("x", "bogus"),
        ]
    )
    assert declared.get("work_at") == "one"
    assert declared.get("address") == "many"
    assert declared.get("mentor") == "one"
    assert "like" not in declared and "x" not in declared
    # Declared wins over the seed list in both directions.
    assert is_single_valued_relation("mentors", declared) is True
    assert is_single_valued_relation("mentors") is False
    assert is_single_valued_relation("addresses", declared) is False


def test_address_verb_is_not_single_valued_by_default() -> None:
    """ "X addresses identity / legacy" must not tie as single-valued: the verb
    stems onto the postal-address noun, so the seed list leaves it out and a
    namespace that stores postal addresses declares cardinality="one"."""
    from server.memory.domain.graph_quality import is_single_valued_relation

    assert is_single_valued_relation("addresses") is False
    assert is_single_valued_relation("address") is False
    assert is_single_valued_relation("home_address") is True  # suffix rule


def test_interval_rules_tie_undated_and_late_arrival() -> None:
    from datetime import datetime

    from server.memory.domain.graph_quality import triple_interval_repairs

    # Both sides carry the STORED (lemmatized) verb, as the writer does.
    existing = [
        {
            "subject": "user",
            "relation": "work_at",
            "object": "acme",
            "valid_from": datetime(2022, 6, 1),
            "valid_to": None,
        },
    ]
    # Tie: same start, different object — the existing row (arrived first) closes.
    tie = triple_interval_repairs(
        existing=existing,
        new_subject="user",
        new_relation="work_at",
        new_object="globex",
        new_valid_at=datetime(2022, 6, 1),
        superseded_at=datetime(2026, 9, 12, 8),
    )
    assert [r.old_object for r in tie] == ["acme"]
    assert tie[0].valid_to == datetime(2022, 6, 1)
    assert tie[0].invalid_at == datetime(2026, 9, 12, 8)
    # Undated new fact: orders by arrival time, so it closes the dated older one.
    undated = triple_interval_repairs(
        existing=existing,
        new_subject="user",
        new_relation="work_at",
        new_object="globex",
        new_valid_at=None,
        new_seen_at=datetime(2026, 9, 12, 8),
    )
    assert [r.old_object for r in undated] == ["acme"]
    assert undated[0].valid_to == datetime(2026, 9, 12, 8)
    # Neither date: nothing deterministic to do.
    assert (
        triple_interval_repairs(
            existing=existing,
            new_subject="user",
            new_relation="work_at",
            new_object="globex",
            new_valid_at=None,
        )
        == []
    )
    # Late-arriving OLDER value: the NEW row is the stale one and closes at the
    # existing fact's start; the existing row is untouched.
    late = triple_interval_repairs(
        existing=existing,
        new_subject="user",
        new_relation="work_at",
        new_object="initech",
        new_valid_at=datetime(2020, 1, 1),
    )
    assert len(late) == 1
    assert late[0].old_object == "initech" and late[0].new_object == "acme"
    assert late[0].valid_to == datetime(2022, 6, 1)
    assert late[0].valid_from == datetime(2020, 1, 1)
    # A later triple in the same batch owns this subject+relation: skip its row.
    batch = triple_interval_repairs(
        existing=existing
        + [
            {
                "subject": "user",
                "relation": "work_at",
                "object": "clickhouse",
                "valid_from": datetime(2024, 3, 1),
                "valid_to": None,
            },
        ],
        new_subject="user",
        new_relation="work_at",
        new_object="globex",
        new_valid_at=datetime(2023, 1, 1),
        later_batch_objects={"clickhouse"},
    )
    assert [r.old_object for r in batch] == ["acme"]


def test_lexical_junk_is_never_a_graph_node() -> None:
    """Fillers, pronoun fragments and light verbs never become entities."""
    from server.memory.domain.graph_quality import canonical_entity_text, is_lexical_junk_entity

    junk = (
        "i m",
        "wow",
        "great",
        "thank",
        "yeah",
        "hey",
        "can t",
        "let s",
        "we re",
        "ok",
        "user",
        "hmm",
        # light verbs, which would otherwise be the best-connected non-entities
        "fix",
        "share",
        "say",
        "add",
        "got",
        "check",
        "keep",
        "make",
        "i ve really",
    )
    for j in junk:
        assert is_lexical_junk_entity(j), j
    assert is_lexical_junk_entity(canonical_entity_text("I'm")) and is_lexical_junk_entity("ab")
    real = (
        "clickhouse",
        "session",
        "column",
        "oliver-mills",
        "config.d",
        "photography",
        "gate b",
        "new york",
        "john",
    )
    for r in real:
        assert not is_lexical_junk_entity(r), r


# ---------------------------------------------------------------------------
# Which value is newer: every row is placed by (start day, seen_at, object).
# ---------------------------------------------------------------------------


def _live(obj: str, valid_from: datetime | None, seen: datetime) -> dict:
    return {
        "subject": "user",
        "relation": "live_in",
        "object": obj,
        "valid_from": valid_from,
        "valid_to": None,
        "seen_at": seen,
    }


def _repairs(existing: list[dict], new: dict, **kw) -> list:
    return triple_interval_repairs(
        existing=existing,
        new_subject="user",
        new_relation="live_in",
        new_object=new["object"],
        new_valid_at=new["valid_from"],
        new_seen_at=new["seen_at"],
        **kw,
    )


def _survivors(rows: list[dict]) -> set[str]:
    """The values none of the writers' repairs closes, when each writer's repair reads
    every row: the values left current."""
    closed = {r.old_object for new in rows for r in _repairs(rows, new)}
    return {row["object"] for row in rows} - closed


def test_three_values_that_cycled_pairwise_leave_one_current_value() -> None:
    # Pairwise, the old rules took A over B, B over C and C over A, and all three closed.
    # By (start day, seen_at): B (Oct 15) > C (Oct 14) > A (Oct 13).
    rows = [
        _live("a", datetime(2026, 10, 13, 12), datetime(2026, 10, 15, 0, 0, 3)),
        _live("b", None, datetime(2026, 10, 15, 0, 0, 2)),
        _live("c", datetime(2026, 10, 14), datetime(2026, 10, 15, 0, 0, 1)),
    ]
    assert _survivors(rows) == {"b"}


@pytest.mark.parametrize(("london_us", "paris_us", "survivor"), [(1, 2, "paris"), (2, 1, "london")])
def test_two_undated_writers_in_one_second_agree_and_the_later_arrival_wins(
    london_us: int, paris_us: int, survivor: str
) -> None:
    second = datetime(2026, 10, 1, 10, 0, 5)
    rows = [
        _live("london", None, second.replace(microsecond=london_us)),
        _live("paris", None, second.replace(microsecond=paris_us)),
    ]
    assert _survivors(rows) == {survivor}


@pytest.mark.parametrize("undated_first", [True, False])
def test_racing_dated_and_undated_writers_agree_on_the_later_day(undated_first: bool) -> None:
    # Rome dated Oct 14 and undated London seen Oct 15, whichever arrived first: the
    # later day wins, so both writers' repairs close Rome.
    london_s, rome_s = (1, 2) if undated_first else (2, 1)
    rows = [
        _live("london", None, datetime(2026, 10, 15, 10, 0, london_s)),
        _live("rome", datetime(2026, 10, 14), datetime(2026, 10, 15, 10, 0, rome_s)),
    ]
    assert _survivors(rows) == {"london"}


def test_a_fact_dated_today_closes_an_undated_value_seen_earlier_today() -> None:
    london = _live("london", None, datetime(2026, 10, 14, 9))
    paris = _live("paris", datetime(2026, 10, 14), datetime(2026, 10, 14, 10))
    repairs = _repairs([london], paris)
    assert [(r.old_object, r.valid_to) for r in repairs] == [("london", datetime(2026, 10, 14))]
    assert _survivors([london, paris]) == {"paris"}


def test_an_undated_update_later_the_same_day_closes_a_dated_value() -> None:
    paris = _live("paris", datetime(2026, 10, 14), datetime(2026, 10, 14, 9))
    london = _live("london", None, datetime(2026, 10, 14, 10, 0, 0, 250))
    repairs = _repairs([paris], london)
    assert [(r.old_object, r.valid_to) for r in repairs] == [("paris", datetime(2026, 10, 14, 10))]
    assert _survivors([london, paris]) == {"london"}


def test_an_undated_value_seen_before_a_past_fact_was_told_stays_current() -> None:
    # London stated undated on Oct 1; on Oct 15 an import says Rome from Jul 1. Rome
    # was over by the time London was seen: London stays current, Rome closes then.
    london = _live("london", None, datetime(2026, 10, 1))
    rome = _live("rome", datetime(2026, 7, 1), datetime(2026, 10, 15))
    repairs = _repairs([london], rome)
    assert [(r.old_object, r.new_object, r.valid_to) for r in repairs] == [
        ("rome", "london", datetime(2026, 10, 1))
    ]


@pytest.mark.parametrize("berlin_is_new", [True, False])
def test_a_future_dated_plan_is_not_closed_before_it_begins(berlin_is_new: bool) -> None:
    # Berlin from Dec 1, stated Oct 1; London stated undated on Oct 5. Whichever repair
    # runs, London closes when Berlin begins.
    berlin = _live("berlin", datetime(2026, 12, 1), datetime(2026, 10, 1))
    london = _live("london", None, datetime(2026, 10, 5))
    new, old = (berlin, london) if berlin_is_new else (london, berlin)
    repairs = _repairs([old], new)
    assert [(r.old_object, r.valid_to) for r in repairs] == [("london", datetime(2026, 12, 1))]


def test_within_one_write_the_later_stated_value_wins() -> None:
    # One write states Zurich, then Amsterdam, on the session date: the write stores
    # Amsterdam a microsecond after Zurich, so Zurich closes though it sorts after it.
    day, seen = datetime(2026, 10, 1), datetime(2026, 10, 1, 10, 0, 0, 7)
    zurich = _live("zurich", day, seen)
    amsterdam = _live("amsterdam", day, seen + timedelta(microseconds=1))
    repairs = _repairs([zurich], amsterdam)
    assert [(r.old_object, r.new_object) for r in repairs] == [("zurich", "amsterdam")]
    assert _survivors([zurich, amsterdam]) == {"amsterdam"}
    # "I lived in London ... I moved to Paris on Oct 1", London left undated: undated,
    # London starts at the write (Oct 15), a later day than Paris, so it is the newer
    # in either order and Paris closes then. Only a date or an end on London keeps
    # Paris current.
    london = _live("london", None, datetime(2026, 10, 15))
    paris = _live("paris", datetime(2026, 10, 1), datetime(2026, 10, 15, 0, 0, 0, 1))
    assert [(r.old_object, r.valid_to) for r in _repairs([london], paris)] == [
        ("paris", datetime(2026, 10, 15))
    ]
    assert _survivors([london, paris]) == {"london"}


@pytest.mark.parametrize("berlin_first", [True, False])
def test_within_one_write_a_later_start_day_wins_whatever_the_order(berlin_first: bool) -> None:
    # One write: "I move to Berlin" (in 45 days) and "I live in Paris" (undated), in
    # either order. Write order only breaks a same-day tie: Paris closes when Berlin
    # begins, and Berlin stays current.
    seen = datetime(2026, 10, 15, 10, 0, 0, 7)
    first, second = seen, seen + timedelta(microseconds=1)
    berlin = _live("berlin", datetime(2026, 11, 29), first if berlin_first else second)
    paris = _live("paris", None, second if berlin_first else first)
    old, new = (berlin, paris) if berlin_first else (paris, berlin)
    repairs = _repairs([old], new)
    assert [(r.old_object, r.valid_to) for r in repairs] == [("paris", datetime(2026, 11, 29))]
    assert _survivors([berlin, paris]) == {"berlin"}


@pytest.mark.parametrize(
    ("acme", "globex", "closed"),
    [
        # Different days: by date, whatever arrived first.
        ((datetime(2026, 9, 1), 2), (datetime(2026, 9, 2), 1), "acme"),
        ((datetime(2026, 9, 2), 1), (datetime(2026, 9, 1), 2), "globex"),
        # One day: by arrival, whatever the hour stated (a deliberate change: the
        # stated time of day no longer orders).
        ((datetime(2026, 9, 1, 18), 1), (datetime(2026, 9, 1, 9), 2), "acme"),
        ((datetime(2026, 9, 1, 9), 2), (datetime(2026, 9, 1, 18), 1), "globex"),
        # One day and one instant: by object text.
        ((datetime(2026, 9, 1), 1), (datetime(2026, 9, 1), 1), "acme"),
    ],
)
def test_dated_values_order_by_day_then_by_arrival(acme, globex, closed) -> None:
    def row(obj: str, start: datetime, us: int) -> dict:
        return {**_live(obj, start, datetime(2026, 9, 12, 10, 0, 0, us)), "relation": "work_at"}

    rows = [row("acme", *acme), row("globex", *globex)]
    for new in rows:
        repairs = triple_interval_repairs(
            existing=rows,
            new_subject="user",
            new_relation="work_at",
            new_object=new["object"],
            new_valid_at=new["valid_from"],
            new_seen_at=new["seen_at"],
        )
        assert [r.old_object for r in repairs] == [closed]


def test_an_undated_value_seen_after_midnight_beats_the_session_days_date() -> None:
    # A behaviour change: a session crossing midnight UTC. London undated, seen at
    # 00:05 on Oct 15, is newer than Paris dated to the session's day, Oct 14, though
    # Paris arrived at 00:10. The day decides, so the order stays total.
    london = _live("london", None, datetime(2026, 10, 15, 0, 5))
    paris = _live("paris", datetime(2026, 10, 14), datetime(2026, 10, 15, 0, 10))
    repairs = _repairs([london], paris)
    assert [(r.old_object, r.new_object, r.valid_to) for r in repairs] == [
        ("paris", "london", datetime(2026, 10, 15, 0, 5))
    ]


def test_a_same_date_tie_gives_each_object_one_verdict() -> None:
    # Plain MergeTree keeps one open row per observation. London seen before and
    # after the new Paris must not both close London and close Paris: one newer row
    # makes the whole value the newer.
    day = datetime(2026, 10, 1)
    london = [_live("london", day, datetime(2026, 10, 1, 10, 0, s)) for s in (0, 5)]
    repairs = _repairs(london, _live("paris", day, datetime(2026, 10, 1, 10, 0, 3)))
    assert {(r.old_object, r.new_object) for r in repairs} == {("paris", "london")}


@pytest.mark.parametrize("new_valid_at", [None, datetime(2026, 10, 1, 10)])
def test_an_undated_value_seen_after_the_write_arrived_is_the_newer_one(new_valid_at) -> None:
    # A replay reuses the arrival of the write it repeats: the undated Paris a later
    # write stated is newer than it and stays current. The older London closes as ever.
    paris_seen = datetime(2026, 10, 1, 10, 0, 5)
    rome = _live("rome", new_valid_at, datetime(2026, 10, 1, 10))
    repairs = _repairs(
        [_live("london", None, datetime(2026, 10, 1, 9)), _live("paris", None, paris_seen)], rome
    )
    assert {(r.old_object, r.new_object, r.valid_to) for r in repairs} == {
        ("london", "rome", datetime(2026, 10, 1, 10)),
        ("rome", "paris", paris_seen),
    }


def test_a_value_restated_with_a_later_date_is_judged_once_by_its_newest_row() -> None:
    # London from March, restated from August; Paris from June arrives. London's
    # August row makes it the newer: Paris closes, and no London row does.
    repairs = _repairs(
        [
            _live("london", datetime(2026, 3, 1), datetime(2026, 9, 1)),
            _live("london", datetime(2026, 8, 1), datetime(2026, 9, 2)),
        ],
        _live("paris", datetime(2026, 6, 1), datetime(2026, 9, 3)),
    )
    assert [(r.old_object, r.new_object, r.valid_to) for r in repairs] == [
        ("paris", "london", datetime(2026, 8, 1))
    ]


def test_a_value_seen_again_undated_keeps_its_dated_start() -> None:
    # Beta from 2023, seen again undated in 2025; Acme from 2020 is told late. Beta is
    # the newer: Acme closes when Beta first began, not when it was seen again.
    def beta(start: datetime | None, seen: datetime) -> dict:
        return {
            "subject": "alice",
            "relation": "works_at",
            "object": "beta",
            "valid_from": start,
            "valid_to": None,
            "seen_at": seen,
        }

    repairs = triple_interval_repairs(
        existing=[
            beta(datetime(2023, 1, 1), datetime(2023, 1, 2)),
            beta(None, datetime(2025, 1, 1)),
        ],
        new_subject="alice",
        new_relation="works_at",
        new_object="acme",
        new_valid_at=datetime(2020, 1, 1),
        new_seen_at=datetime(2026, 10, 1),
    )
    assert [(r.old_object, r.new_object, r.valid_to) for r in repairs] == [
        ("acme", "beta", datetime(2023, 1, 1))
    ]


def test_every_start_of_an_older_value_is_closed() -> None:
    # Each start closes its own edge interval.
    repairs = _repairs(
        [
            _live("london", datetime(2026, 1, 1), datetime(2026, 9, 1)),
            _live("london", datetime(2026, 3, 1), datetime(2026, 9, 1)),
        ],
        _live("paris", datetime(2026, 6, 1), datetime(2026, 9, 2)),
    )
    assert sorted((r.old_object, r.valid_from, r.valid_to) for r in repairs) == [
        ("london", datetime(2026, 1, 1), datetime(2026, 6, 1)),
        ("london", datetime(2026, 3, 1), datetime(2026, 6, 1)),
    ]


def test_an_undated_row_is_judged_by_when_it_was_seen() -> None:
    # London was stated undated on Feb 1; Rome from Jul 1 arrives on Oct 15 and
    # London closes then. A London row dated 2025 and seen on Oct 10 is older too:
    # each row starts at its own date, or undated, when it was seen.
    undated = _live("london", None, datetime(2026, 2, 1))
    dated = _live("london", datetime(2025, 1, 1), datetime(2026, 10, 10))
    rome = _live("rome", datetime(2026, 7, 1), datetime(2026, 10, 15))

    def closes(existing: list[dict]) -> list[tuple]:
        return sorted(
            (r.old_object, r.valid_from or datetime.min, r.valid_to)
            for r in _repairs(existing, rome)
        )

    assert closes([undated]) == [("london", datetime.min, datetime(2026, 7, 1))]
    assert closes([undated, dated]) == [
        ("london", datetime.min, datetime(2026, 7, 1)),
        ("london", datetime(2025, 1, 1), datetime(2026, 7, 1)),
    ]


def test_a_repair_carries_the_arrival_of_the_value_that_closed() -> None:
    # The replay judges a restatement against the closing value's own arrival.
    seen = datetime(2026, 10, 1, 10, 0, 0, 42)
    (closed,) = _repairs([_live("london", None, datetime(2026, 9, 1))], _live("rome", None, seen))
    assert closed.new_seen_at == seen
    (late,) = _repairs([_live("london", None, seen)], _live("rome", datetime(2026, 7, 1), seen))
    assert (late.old_object, late.new_seen_at) == ("rome", seen)
