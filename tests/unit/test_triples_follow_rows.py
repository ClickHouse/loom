"""The triple plane follows the row plane's supersession. Pure planning
against the MemoryAgentBench shapes (stale open triple, update row active
under another key)."""

from __future__ import annotations

from server.memory.domain import triples_follow_rows as tfr


def test_entity_in_tolerates_backfill_stems() -> None:
    text = "The chairperson of Fatah is Mahmoud Abbas."
    assert tfr.entity_in(text, "mahmoud abba")  # the graph's stem
    assert tfr.entity_in(text, "fatah")
    assert not tfr.entity_in(text, "moshe kahlon")
    assert tfr.entity_in("The director of The Beatles is Joe Hockey.", "beatle")
    assert not tfr.entity_in("anything", "")


def test_substituted_spans_finds_the_one_differing_span() -> None:
    assert tfr.substituted_spans(
        "The chairperson of Fatah is Mahmoud Abbas.", "The chairperson of Fatah is Moshe Kahlon."
    ) == ("mahmoud abbas", "moshe kahlon")
    assert tfr.substituted_spans(
        "Basketball was created in the country of United States of America.",
        "Basketball was created in the country of Soviet Union.",
    ) == ("united states of america", "soviet union")
    # a subject swap is a span too
    assert tfr.substituted_spans("Steve Sax likes baseball.", "Bo Ryan likes baseball.") == (
        "steve sax",
        "bo ryan",
    )
    # the sweep's lead-in is not part of the shape
    assert tfr.substituted_spans(
        "Thomas Arne was born in the city of London.",
        "User stated that Thomas Arne was born in the city of Bengaluru.",
    ) == ("london", "bengaluru")
    # not the same shape: nothing to copy
    assert tfr.substituted_spans("Alice moved to Paris.", "Bob likes tea a lot.") is None
    assert tfr.substituted_spans("same text", "same text") is None
    assert tfr.substituted_spans("Alice", "Alice moved") is None


def test_plan_closes_the_stale_triple_and_writes_the_successor() -> None:
    open_triples = [
        {
            "subject": "fatah",
            "relation": "chairperson_of",
            "object": "mahmoud abba",
            "confidence": 0.9,
            "provenance": "extracted",
        },
        {"subject": "mahmoud abba", "relation": "profession", "object": "politician"},
        {"subject": "steve sax", "relation": "associ_with_sport", "object": "baseball"},
    ]
    pairs = [
        tfr.RowSupersession(
            "The chairperson of Fatah is Mahmoud Abbas.",
            "The chairperson of Fatah is Moshe Kahlon.",
            winner_key="moshe_kahlon_chairperson",
        ),
        tfr.RowSupersession(
            "Steve Sax is associated with the sport of baseball.",
            "Steve Sax is associated with the sport of association football.",
            winner_key="steve_sax_association",
        ),
    ]
    plan = tfr.plan_follow(open_triples, pairs)
    assert plan.closes == [
        ("fatah", "chairperson_of", "mahmoud abba"),
        ("steve sax", "associ_with_sport", "baseball"),
    ]
    # Abbas's own profession is not the superseded row's business
    assert ("mahmoud abba", "profession", "politician") not in plan.closes
    assert [(s, r, o, k) for s, r, o, k, _vf, _c, _p in plan.inserts] == [
        ("fatah", "chairperson_of", "moshe kahlon", "moshe_kahlon_chairperson"),
        ("steve sax", "associ_with_sport", "association football", "steve_sax_association"),
    ]
    assert plan.inserts[0][5] == 0.9  # confidence copied from the closed triple
    assert plan.inserts[0][6] == "extracted"  # and its provenance
    assert plan.inserts[1][6] == ""  # a predecessor without one passes none on


def test_plan_keeps_triples_the_successor_still_attests_and_skips_open_successors() -> None:
    open_triples = [
        {"subject": "amala paul", "relation": "citizen_of", "object": "india"},
        {"subject": "amala paul", "relation": "citizen_of", "object": "belgium"},
    ]
    # an elaboration keeps the value: nothing closes
    plan = tfr.plan_follow(
        open_triples[:1],
        [
            tfr.RowSupersession(
                "Amala Paul is a citizen of India.",
                "Amala Paul, an actress, is a citizen of India.",
            )
        ],
    )
    assert plan.closes == [] and plan.inserts == []
    # the successor triple already open is not written twice
    plan = tfr.plan_follow(
        open_triples,
        [
            tfr.RowSupersession(
                "Amala Paul is a citizen of India.", "Amala Paul is a citizen of Belgium."
            )
        ],
    )
    assert plan.closes == [("amala paul", "citizen_of", "india")]
    assert plan.inserts == []


def test_plan_closes_without_a_successor_when_the_shape_differs() -> None:
    plan = tfr.plan_follow(
        [{"subject": "thomas arne", "relation": "born_in", "object": "london"}],
        [
            tfr.RowSupersession(
                "Thomas Arne was born in the city of London.",
                "Thomas Arne: born and raised in Bengaluru, Karnataka.",
            )
        ],
    )
    assert plan.closes == [("thomas arne", "born_in", "london")]
    assert plan.inserts == []


def test_object_substitution_requires_a_shared_subject_and_predicate() -> None:
    """A one-span rule would pair two subjects sharing an object inside one
    write and supersede the update row itself."""
    assert tfr.object_substitution(
        "The capital of Soviet Union is Moscow.", "The capital of Soviet Union is Russellville."
    ) == ("moscow", "russellville")
    assert tfr.object_substitution(
        "Basketball was created in the country of United States of America.",
        "Basketball was created in the country of Soviet Union.",
    ) == ("united states of america", "soviet union")
    assert tfr.object_substitution(
        "Moshe Kahlon works in the field of politician.",
        "User stated that Moshe Kahlon works in the field of soldier.",
    ) == ("politician", "soldier")
    # two subjects, one object: not an update
    assert (
        tfr.object_substitution(
            "Basketball was created in the country of Soviet Union.",
            "Club Nouveau was created in the country of Soviet Union.",
        )
        is None
    )
    assert (
        tfr.object_substitution(
            "The capital of Great Britain is London.", "The capital of United Kingdom is London."
        )
        is None
    )
    # a shared beginning with a different subject inside the span
    assert (
        tfr.object_substitution(
            "The capital of Soviet Union is Moscow.", "The capital of Italy is Rome."
        )
        is None
    )
    assert tfr.object_substitution("Steve Sax likes baseball.", "Bo Ryan likes baseball.") is None
