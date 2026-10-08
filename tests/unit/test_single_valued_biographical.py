"""Biographical attributes are single-valued at a time."""

from __future__ import annotations

import pytest

from server.memory.domain.graph_quality import _lemma_relation, is_single_valued_relation


@pytest.mark.parametrize(
    "surface",
    [
        "born_in",
        "birthplace",
        "died_in",
        "death_place",
        "place_of_death",
        "citizen_of",
        "citizenship",
        "nationality",
        "headquartered_in",
        "headquarters",
        "capital",
        "capital_of",
        "ceo",
        "chief_executive_officer",
        "head_of_government",
        "head_of_state",
        "married_to",
        "spouse",
        "plays_position",
        "created_in",
    ],
)
def test_biographical_attributes_close_older_values(surface: str) -> None:
    # probed in the STORED (lemma) form, as the write path does
    assert is_single_valued_relation(_lemma_relation(surface))


@pytest.mark.parametrize(
    "surface",
    [
        "ceo_of",
        "chairperson_of",
        "director_of",
        "head_of",
        "prime_minister_of",
        "president_of",
        "associated_with_sport",
        "affiliated_with_religion",
        "broadcast_by",
        "position",
    ],
)
def test_functional_roles_close_older_holders(surface: str) -> None:
    # The verbs the relation backfill emits for offices and
    # one-at-a-time affiliations, probed in the stored lemma form.
    assert is_single_valued_relation(_lemma_relation(surface))


@pytest.mark.parametrize(
    "surface",
    [
        "founded_by",
        "author",
        "author_of",
        "associated_with",
        "educated_at",
        "member_of",
        "developed_by",
        "produced_by",
        "performed_by",
        "speaks_language",
        "official_language",
    ],
)
def test_relations_with_real_multiplicity_stay_open(surface: str) -> None:
    assert not is_single_valued_relation(_lemma_relation(surface))


def test_a_namespace_declaration_still_wins() -> None:
    from server.memory.domain.graph_quality import declared_cardinality

    declared = declared_cardinality([("born_in", "many")])
    assert not is_single_valued_relation(_lemma_relation("born_in"), declared)
