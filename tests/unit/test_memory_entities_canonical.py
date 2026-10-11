"""Entities are canonical where they enter a row (the writer's input, the extractor's
reply), in the form a search's entity filter is canonicalised to. Canonicalisation is
idempotent, so a row that already holds canonical text keeps it."""

from __future__ import annotations

import pytest

from server.memory.domain.graph_quality import canonical_entities, canonical_entity_text
from server.memory.workers.entity_extractor import _parse_entities


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("api responses", "api response"),
        ("sql databases", "sql database"),
        ("Billing Services", "billing service"),
        ("sizes", "size"),
        ("classes", "class"),
        ("buzzes", "buzz"),
        ("boxes", "box"),
        ("parties", "party"),
        ("shelves", "shelf"),
        ("The Ledger's", "ledger"),
        ("status", "status"),
    ],
)
def test_canonical_text_is_a_fixed_point(raw: str, canonical: str) -> None:
    assert canonical_entity_text(raw) == canonical
    assert canonical_entity_text(canonical) == canonical


def test_canonical_entities_fold_surface_forms_once_in_order() -> None:
    assert canonical_entities(["Billing Services", "billing service", "The Ledger", ""]) == [
        "billing service",
        "ledger",
    ]


def test_the_extractor_reply_is_canonical() -> None:
    assert _parse_entities('["API Responses", "api response", "Kafka"]') == [
        "api response",
        "kafka",
    ]


@pytest.mark.parametrize(
    ("plural", "singular"),
    [
        ("buses", "bus"),
        ("gases", "gas"),
        ("lenses", "lens"),
        ("irises", "iris"),
        ("statuses", "status"),
        ("viruses", "virus"),
        ("classes", "class"),
        ("aliases", "alias"),
        ("responses", "response"),
        ("databases", "database"),
        ("cases", "case"),
        ("uses", "use"),
        ("sizes", "size"),
        ("buzzes", "buzz"),
        ("waltzes", "waltz"),
        ("kits", "kit"),
        ("parties", "party"),
        ("boxes", "box"),
        ("breaches", "breach"),
        ("shelves", "shelf"),
    ],
)
def test_a_plural_and_its_singular_are_one_entity(plural: str, singular: str) -> None:
    assert canonical_entity_text(plural) == canonical_entity_text(singular) == singular


@pytest.mark.parametrize("raw", ['"The Ledger"', "(The Ledger)", "the the ledger", "[a] ledger"])
def test_an_article_behind_punctuation_is_dropped_once(raw: str) -> None:
    assert canonical_entity_text(raw) == "ledger"
    assert canonical_entity_text(canonical_entity_text(raw)) == "ledger"


@pytest.mark.parametrize(
    ("stored", "now"),
    [
        ("api respons", "api response"),
        ("payload siz", "payload size"),
        ("cas", "case"),
        ("the ledger", "ledger"),
        ("bus", "bus"),
        ("status", "status"),
        ("kit", "kit"),
    ],
)
def test_text_an_earlier_rule_stored_moves_to_todays_form(stored: str, now: str) -> None:
    from server.memory.domain.graph_quality import recanonical

    assert recanonical(stored) == now
