from datetime import datetime

import pytest

from server.memory.domain.graph_quality import (
    canonical_edge_pair,
    canonical_entity,
    canonical_entity_text,
    canonical_relation,
    is_single_valued_relation,
    should_invalidate_interval,
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


def test_should_invalidate_open_interval_only_when_newer_fact_is_dated() -> None:
    assert should_invalidate_interval(
        existing_valid_at=datetime(2024, 1, 1),
        existing_valid_to=None,
        new_valid_at=datetime(2025, 1, 1),
    )
    assert not should_invalidate_interval(
        existing_valid_at=datetime(2024, 1, 1),
        existing_valid_to=datetime(2024, 6, 1),
        new_valid_at=datetime(2025, 1, 1),
    )
    assert not should_invalidate_interval(
        existing_valid_at=datetime(2024, 1, 1),
        existing_valid_to=None,
        new_valid_at=None,
    )


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
        arrival_at=datetime(2026, 9, 12, 8),
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


@pytest.mark.parametrize(
    ("acme_at", "globex_at", "closed"),
    [(1, 1, "acme"), (1, 2, "acme"), (2, 1, "globex")],
)
def test_two_writers_racing_on_one_date_leave_one_current_value(acme_at, globex_at, closed) -> None:
    """Each writer reads the other's row: the later arrival stays current (by second,
    then by object), and both repairs close the same row."""
    from datetime import datetime

    from server.memory.domain.graph_quality import triple_interval_repairs

    day = datetime(2026, 9, 1)

    def row(obj: str, second: int) -> dict:
        seen = datetime(2026, 9, 12, 10, 0, second)
        return {"subject": "user", "relation": "work_at", "object": obj, "valid_from": day,
                "valid_to": None, "last_seen": seen}  # fmt: skip

    rows = [row("acme", acme_at), row("globex", globex_at)]
    for new in rows:
        repairs = triple_interval_repairs(
            existing=rows,
            new_subject="user",
            new_relation="work_at",
            new_object=new["object"],
            new_valid_at=day,
            new_seen_at=new["last_seen"],
        )
        assert [r.old_object for r in repairs] == [closed]


def test_a_same_date_tie_gives_each_object_one_verdict() -> None:
    # Plain MergeTree keeps one open row per observation. London seen before and
    # after the new Paris must not both close London and close Paris: the object's
    # latest row decides for all of its rows.
    day = datetime(2026, 10, 1)

    def london(second: int) -> dict:
        return {"subject": "user", "relation": "live_in", "object": "london",
                "valid_from": day, "valid_to": None,
                "last_seen": datetime(2026, 10, 1, 10, 0, second)}  # fmt: skip

    repairs = triple_interval_repairs(
        existing=[london(0), london(5)],
        new_subject="user",
        new_relation="live_in",
        new_object="paris",
        new_valid_at=day,
        new_seen_at=datetime(2026, 10, 1, 10, 0, 3),
    )
    assert {(r.old_object, r.new_object) for r in repairs} == {("paris", "london")}


@pytest.mark.parametrize("new_valid_at", [None, datetime(2026, 10, 1, 10)])
def test_an_undated_value_seen_after_the_write_arrived_is_the_newer_one(new_valid_at) -> None:
    # A replay is dated at the write it repeats: the undated Paris a later write
    # stated is newer than it and stays current. The older London closes as ever.
    def row(obj: str, seen: datetime) -> dict:
        return {"subject": "user", "relation": "live_in", "object": obj,
                "valid_from": None, "valid_to": None, "last_seen": seen}  # fmt: skip

    paris_seen = datetime(2026, 10, 1, 10, 0, 5)
    repairs = triple_interval_repairs(
        existing=[row("london", datetime(2026, 10, 1, 9)), row("paris", paris_seen)],
        new_subject="user",
        new_relation="live_in",
        new_object="rome",
        new_valid_at=new_valid_at,
        arrival_at=datetime(2026, 10, 1, 10),
    )
    assert {(r.old_object, r.new_object, r.valid_to) for r in repairs} == {
        ("london", "rome", datetime(2026, 10, 1, 10)),
        ("rome", "paris", paris_seen),
    }


def test_a_same_date_value_earlier_in_the_batch_closes_whatever_it_was_seen() -> None:
    # One write states Zurich, then Amsterdam, on the session date: both rows carry the
    # write's second, and Zurich, written first, closes though it sorts after Amsterdam.
    day = datetime(2026, 10, 1)
    seen = datetime(2026, 10, 1, 10)
    zurich = {"subject": "user", "relation": "work_at", "object": "zurich",
              "valid_from": day, "valid_to": None, "last_seen": seen}  # fmt: skip
    repairs = triple_interval_repairs(
        existing=[zurich],
        new_subject="user",
        new_relation="work_at",
        new_object="amsterdam",
        new_valid_at=day,
        arrival_at=seen,
        new_seen_at=seen,
        earlier_batch_objects={"zurich"},
    )
    assert [(r.old_object, r.new_object) for r in repairs] == [("zurich", "amsterdam")]


def test_an_undated_value_seen_after_the_write_does_not_close_a_value_not_begun() -> None:
    # The Oct 1 write said Berlin from Dec 1; London was stated undated on Oct 5. Its
    # replay closes London when Berlin begins, not Berlin before it begins.
    london = {"subject": "user", "relation": "live_in", "object": "london",
              "valid_from": None, "valid_to": None, "last_seen": datetime(2026, 10, 5)}  # fmt: skip
    repairs = triple_interval_repairs(
        existing=[london],
        new_subject="user",
        new_relation="live_in",
        new_object="berlin",
        new_valid_at=datetime(2026, 12, 1),
        arrival_at=datetime(2026, 10, 1),
    )
    assert [(r.old_object, r.valid_to) for r in repairs] == [("london", datetime(2026, 12, 1))]


def test_a_fact_dated_today_closes_an_undated_value_seen_earlier_today() -> None:
    london = {"subject": "user", "relation": "live_in", "object": "london",
              "valid_from": None, "valid_to": None,
              "last_seen": datetime(2026, 10, 14, 9)}  # fmt: skip
    repairs = triple_interval_repairs(
        existing=[london],
        new_subject="user",
        new_relation="live_in",
        new_object="paris",
        new_valid_at=datetime(2026, 10, 14),
        arrival_at=datetime(2026, 10, 14, 10),
    )
    assert [(r.old_object, r.valid_to) for r in repairs] == [("london", datetime(2026, 10, 14))]


@pytest.mark.parametrize(
    ("london_seen", "paris_from", "arrival", "earlier"),
    [
        # "I lived in London ... I moved to Paris on Oct 1", in one write.
        (datetime(2026, 10, 15), datetime(2026, 10, 1), datetime(2026, 10, 15), {"london"}),
        # A session across midnight UTC: Paris carries the session's day.
        (
            datetime(2026, 10, 15, 0, 5),
            datetime(2026, 10, 14),
            datetime(2026, 10, 15, 0, 10),
            set(),
        ),
    ],
)
def test_an_undated_value_seen_before_the_write_is_older_whatever_its_day(
    london_seen, paris_from, arrival, earlier
) -> None:
    london = {"subject": "user", "relation": "live_in", "object": "london",
              "valid_from": None, "valid_to": None, "last_seen": london_seen}  # fmt: skip
    repairs = triple_interval_repairs(
        existing=[london],
        new_subject="user",
        new_relation="live_in",
        new_object="paris",
        new_valid_at=paris_from,
        arrival_at=arrival,
        earlier_batch_objects=earlier,
    )
    assert [(r.old_object, r.valid_to) for r in repairs] == [("london", paris_from)]


def test_a_fact_dated_well_before_an_undated_value_was_seen_is_the_older() -> None:
    # London stated undated on Oct 1; on Oct 15 an import says Rome from Jul 1. Rome
    # was over by the time London was seen: London stays current, Rome closes then.
    london = {"subject": "user", "relation": "live_in", "object": "london",
              "valid_from": None, "valid_to": None, "last_seen": datetime(2026, 10, 1)}  # fmt: skip
    repairs = triple_interval_repairs(
        existing=[london],
        new_subject="user",
        new_relation="live_in",
        new_object="rome",
        new_valid_at=datetime(2026, 7, 1),
        arrival_at=datetime(2026, 10, 15),
    )
    assert [(r.old_object, r.new_object, r.valid_to) for r in repairs] == [
        ("rome", "london", datetime(2026, 10, 1))
    ]


def test_a_value_restated_with_a_later_date_is_judged_once_by_its_newest_row() -> None:
    # London from March, restated from August; Paris from June arrives. London's
    # August row makes it the newer: Paris closes, and no London row does.
    def london(start: datetime, seen: datetime) -> dict:
        return {"subject": "user", "relation": "live_in", "object": "london",
                "valid_from": start, "valid_to": None, "last_seen": seen}  # fmt: skip

    repairs = triple_interval_repairs(
        existing=[
            london(datetime(2026, 3, 1), datetime(2026, 9, 1)),
            london(datetime(2026, 8, 1), datetime(2026, 9, 2)),
        ],
        new_subject="user",
        new_relation="live_in",
        new_object="paris",
        new_valid_at=datetime(2026, 6, 1),
        arrival_at=datetime(2026, 9, 3),
        new_seen_at=datetime(2026, 9, 3),
    )
    assert [(r.old_object, r.new_object, r.valid_to) for r in repairs] == [
        ("paris", "london", datetime(2026, 8, 1))
    ]


def test_two_undated_writers_in_one_second_agree_on_one_current_value() -> None:
    # Each write's repair reads the other's open row from the same second: both take
    # the same value for the newer (by object text), so exactly one stays current.
    seen = datetime(2026, 10, 1, 10, 0, 5)

    def repair(new: str, other: str) -> set[tuple[str, str]]:
        row = {"subject": "user", "relation": "live_in", "object": other,
               "valid_from": None, "valid_to": None, "last_seen": seen}  # fmt: skip
        repairs = triple_interval_repairs(
            existing=[row],
            new_subject="user",
            new_relation="live_in",
            new_object=new,
            new_valid_at=None,
            arrival_at=seen,
            new_seen_at=seen,
        )
        return {(r.old_object, r.new_object) for r in repairs}

    assert repair("london", "paris") == repair("paris", "london") == {("london", "paris")}


def test_a_value_seen_again_undated_keeps_its_dated_start() -> None:
    # Beta from 2023, seen again undated in 2025; Acme from 2020 is told late. Beta's
    # dated row still makes it the newer: Acme closes when Beta began.
    def beta(start: datetime | None, seen: datetime) -> dict:
        return {"subject": "alice", "relation": "works_at", "object": "beta",
                "valid_from": start, "valid_to": None, "last_seen": seen}  # fmt: skip

    repairs = triple_interval_repairs(
        existing=[
            beta(datetime(2023, 1, 1), datetime(2023, 1, 2)),
            beta(None, datetime(2025, 1, 1)),
        ],
        new_subject="alice",
        new_relation="works_at",
        new_object="acme",
        new_valid_at=datetime(2020, 1, 1),
        arrival_at=datetime(2026, 10, 1),
        new_seen_at=datetime(2026, 10, 1),
    )
    assert [(r.old_object, r.new_object, r.valid_to) for r in repairs] == [
        ("acme", "beta", datetime(2023, 1, 1))
    ]


def test_every_start_of_an_older_value_is_closed() -> None:
    # Each start closes its own edge interval.
    def london(start: datetime) -> dict:
        return {"subject": "user", "relation": "live_in", "object": "london",
                "valid_from": start, "valid_to": None, "last_seen": datetime(2026, 9, 1)}  # fmt: skip

    repairs = triple_interval_repairs(
        existing=[london(datetime(2026, 1, 1)), london(datetime(2026, 3, 1))],
        new_subject="user",
        new_relation="live_in",
        new_object="paris",
        new_valid_at=datetime(2026, 6, 1),
        arrival_at=datetime(2026, 9, 2),
        new_seen_at=datetime(2026, 9, 2),
    )
    assert sorted((r.old_object, r.valid_from, r.valid_to) for r in repairs) == [
        ("london", datetime(2026, 1, 1), datetime(2026, 6, 1)),
        ("london", datetime(2026, 3, 1), datetime(2026, 6, 1)),
    ]


def test_an_undated_row_is_judged_by_when_it_was_seen() -> None:
    # London was stated undated on Feb 1; Rome from Jul 1 arrives on Oct 15 and
    # London closes then. A London row dated 2025 and seen on Oct 10 says nothing
    # about when the undated row was stated: London still closes on Jul 1.
    def london(start: datetime | None, seen: datetime) -> dict:
        return {"subject": "user", "relation": "live_in", "object": "london",
                "valid_from": start, "valid_to": None, "last_seen": seen}  # fmt: skip

    undated = london(None, datetime(2026, 2, 1))
    dated = london(datetime(2025, 1, 1), datetime(2026, 10, 10))

    def closes(existing: list[dict]) -> list[tuple]:
        repairs = triple_interval_repairs(
            existing=existing,
            new_subject="user",
            new_relation="live_in",
            new_object="rome",
            new_valid_at=datetime(2026, 7, 1),
            arrival_at=datetime(2026, 10, 15),
            new_seen_at=datetime(2026, 10, 15),
        )
        return sorted((r.old_object, r.valid_from or datetime.min, r.valid_to) for r in repairs)

    assert closes([undated]) == [("london", datetime.min, datetime(2026, 7, 1))]
    assert closes([undated, dated]) == [
        ("london", datetime.min, datetime(2026, 7, 1)),
        ("london", datetime(2025, 1, 1), datetime(2026, 7, 1)),
    ]


@pytest.mark.parametrize(
    ("london_seen", "rome_seen", "survivor"),
    [
        # One second: by object text. Two seconds: the later arrival.
        (datetime(2026, 10, 15, 10, 0, 5), datetime(2026, 10, 15, 10, 0, 5), "rome"),
        (datetime(2026, 10, 15, 10, 0, 5), datetime(2026, 10, 15, 10, 0, 6), "rome"),
        (datetime(2026, 10, 15, 10, 0, 6), datetime(2026, 10, 15, 10, 0, 5), "london"),
    ],
)
def test_racing_dated_and_undated_writers_agree_on_one_current_value(
    london_seen: datetime, rome_seen: datetime, survivor: str
) -> None:
    # Undated London and Rome dated the day before are written together, and each
    # repair reads the other's row. Within the session-date tolerance the two order by
    # arrival, so both repairs close the same value and one stays current.
    london = {"subject": "user", "relation": "live_in", "object": "london",
              "valid_from": None, "valid_to": None, "last_seen": london_seen}  # fmt: skip
    rome = {"subject": "user", "relation": "live_in", "object": "rome",
            "valid_from": datetime(2026, 10, 14), "valid_to": None, "last_seen": rome_seen}  # fmt: skip

    def closed(new: dict, other: dict) -> set[str]:
        repairs = triple_interval_repairs(
            existing=[other],
            new_subject="user",
            new_relation="live_in",
            new_object=new["object"],
            new_valid_at=new["valid_from"],
            arrival_at=new["last_seen"],
            new_seen_at=new["last_seen"],
        )
        return {r.old_object for r in repairs}

    loser = {"london", "rome"} - {survivor}
    assert closed(rome, london) == closed(london, rome) == loser
