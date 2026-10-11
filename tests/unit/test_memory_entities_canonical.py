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
