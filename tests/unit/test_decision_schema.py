"""The decision predicates: which rows are decisions, and which are read whole."""

from __future__ import annotations

import pytest

from server.memory.domain import decision_schema as schema

# memory_type, tags, is_decision, is_whole_row
ROWS = [
    pytest.param("decision", [], True, True, id="type-only"),
    pytest.param("fact", ["decision"], True, True, id="tag-only"),
    pytest.param("decision", ["decision"], True, True, id="type-and-tag"),
    pytest.param("fact", ["constraint"], False, True, id="constraint-tag-only"),
    pytest.param("decision", ["constraint"], True, True, id="type-and-constraint-tag"),
    pytest.param("fact", [], False, False, id="neither"),
    pytest.param("", [], False, False, id="empty-type"),
    pytest.param("fact", ["deploy", "source-changed"], False, False, id="other-tags"),
    # A tag counts only when it is the marker itself: the outcome and stage tags a
    # decision row also carries start with the marker and are not it.
    pytest.param("fact", ["decision:success"], False, False, id="look-alike-success"),
    pytest.param("fact", ["decision:failure"], False, False, id="look-alike-failure"),
    pytest.param("fact", ["decision:outcome-known"], False, False, id="look-alike-outcome"),
    pytest.param("fact", ["decision:proposed"], False, False, id="look-alike-proposed"),
    pytest.param("fact", ["constraint:diet"], False, False, id="look-alike-constraint"),
    # Neither the type nor a tag matches in another case or as part of a longer word.
    pytest.param("Decision", ["Decision", "Constraint"], False, False, id="other-case"),
    pytest.param("decisions", ["decisions", "constraints"], False, False, id="plural"),
]


@pytest.mark.parametrize(("memory_type", "tags", "decision", "whole"), ROWS)
def test_a_row_is_a_decision_by_its_type_or_its_tag_and_whole_as_one_or_a_constraint(
    memory_type: str, tags: list[str], decision: bool, whole: bool
) -> None:
    assert schema.is_decision(memory_type, tags) is decision
    assert schema.is_whole_row(memory_type, tags) is whole


@pytest.mark.parametrize("collection", [list, tuple, set, frozenset])
def test_the_tags_are_read_from_any_collection(collection: type) -> None:
    assert schema.is_decision("fact", collection(["deploy", "decision"]))
    assert schema.is_whole_row("fact", collection(["deploy", "constraint"]))
    assert not schema.is_whole_row("fact", collection(["deploy", "decision:success"]))


def test_the_look_alike_tags_are_the_ones_a_decision_row_carries() -> None:
    # The table above spells the tags out; this ties them to the module's constants.
    assert (schema.SCHEMA_NAME, schema.DECISION_TAG, schema.CONSTRAINT_TAG) == (
        "decision",
        "decision",
        "constraint",
    )
    assert {
        schema.SUCCESS_TAG,
        schema.FAILURE_TAG,
        schema.OUTCOME_KNOWN_TAG,
        schema.PROPOSED_TAG,
    } == {"decision:success", "decision:failure", "decision:outcome-known", "decision:proposed"}
