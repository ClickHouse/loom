"""Templated substitutions inside one write supersede each other."""

from __future__ import annotations

from server.memory.domain.conflicts import intra_batch_supersessions


def test_later_row_of_a_templated_pair_supersedes_the_earlier() -> None:
    values = [
        "The capital of Soviet Union is Moscow.",
        "Basketball was created in the country of United States of America.",
        "The capital of Soviet Union is Russellville.",
        "Basketball was created in the country of Soviet Union.",
        "Alice likes tea.",
    ]
    assert intra_batch_supersessions(values) == [(0, 2), (1, 3)]
    # two subjects sharing an object inside one write are not a pair
    assert (
        intra_batch_supersessions(
            [
                "Basketball was created in the country of Soviet Union.",
                "Club Nouveau was created in the country of Soviet Union.",
                "The capital of Great Britain is London.",
                "The capital of United Kingdom is London.",
            ]
        )
        == []
    )
    # a different memory type never pairs
    assert intra_batch_supersessions(
        values, ["semantic", "semantic", "episodic", "semantic", "semantic"]
    ) == [(1, 3)]
    assert intra_batch_supersessions([]) == []
    assert intra_batch_supersessions(["same", "same"]) == []


def test_stamping_keeps_every_row_on_one_key_set() -> None:
    """`ch.insert` derives the columns from row 0: a key present on some rows
    only fails the batch (every chunk of a fresh ingest 500'd)."""
    from datetime import datetime

    from server.memory.domain.conflicts import stamp_intra_batch_supersessions

    rows = [
        {
            "memory_id": "a",
            "memory_key": "k1",
            "value": "The capital of Soviet Union is Moscow.",
            "status": "active",
            "valid_at": None,
        },
        {
            "memory_id": "b",
            "memory_key": "k2",
            "value": "Alice likes tea.",
            "status": "active",
            "valid_at": None,
        },
        {
            "memory_id": "c",
            "memory_key": "k3",
            "value": "The capital of Soviet Union is Russellville.",
            "status": "active",
            "valid_at": datetime(2024, 1, 2),
        },
    ]
    embs = [
        {"memory_key": "k1", "status": "active", "invalid_at": None},
        {"memory_key": "k3", "status": "active", "invalid_at": None},
    ]
    got = stamp_intra_batch_supersessions(rows, embs, [(0, 2)], datetime(2026, 1, 1))
    assert got == [("a", "c")]
    assert rows[0]["status"] == "superseded" and rows[0]["superseded_by"] == "c"
    assert rows[0]["invalid_at"] == datetime(2024, 1, 2)
    assert embs[0]["status"] == "superseded" and embs[0]["invalid_at"] == datetime(2024, 1, 2)
    assert len({tuple(sorted(r.keys())) for r in rows}) == 1  # one key set for the insert
    assert rows[1]["status"] == "active" and rows[1]["superseded_by"] == ""


def test_parenthetical_aside_does_not_make_an_update_a_restatement() -> None:
    from server.memory.domain.conflicts import (
        same_content_words,
        strip_parentheticals,
        templated_substitution,
    )

    old = "Basketball was created in the country of United States of America."
    new = "Basketball was created in the country of Soviet Union (updated from United States of America)."
    assert strip_parentheticals(new) == "Basketball was created in the country of Soviet Union."
    assert not same_content_words(old, new)
    assert templated_substitution(old, new)
    assert same_content_words("PR 1 has a bug", "PR 1 has a bug (see the null check)")


def test_sweep_lead_in_is_stripped_from_values() -> None:
    from server.memory.app.extract.sweep import strip_statement_lead_in

    assert (
        strip_statement_lead_in("User stated that the capital of Italy is Duluth.")
        == "The capital of Italy is Duluth."
    )
    assert (
        strip_statement_lead_in("The user said that Thomas Arne was born in Bengaluru.")
        == "Thomas Arne was born in Bengaluru."
    )
    assert (
        strip_statement_lead_in("Thomas Arne was born in Bengaluru.")
        == "Thomas Arne was born in Bengaluru."
    )
    assert strip_statement_lead_in("User stated that no.") == "User stated that no."


def test_two_facts_of_a_many_valued_attribute_never_pair() -> None:
    import json

    assert intra_batch_supersessions(["I have a dog.", "I have a cat."]) == []
    # The rule knows English function words only, so it reads English sentences only.
    russian = ["У меня есть собака.", "У меня есть кошка."]
    assert intra_batch_supersessions(russian) == []
    assert intra_batch_supersessions([json.dumps(v) for v in russian]) == []
    french = ["Wang Xiaoming est âgé de 28 ans.", "Wang Xiaoming est ingénieur logiciel."]
    assert intra_batch_supersessions(french) == []


def test_two_predicates_of_one_object_never_pair() -> None:
    lives_and_works = ["The user currently lives in London.", "The user currently works in London."]
    assert intra_batch_supersessions(lives_and_works) == []
