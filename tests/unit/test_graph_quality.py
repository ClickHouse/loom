from datetime import datetime

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
