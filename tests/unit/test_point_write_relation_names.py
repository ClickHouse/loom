"""A point write's `relations` are checked in the canonical form the triple writer keeps."""

import pytest
from pydantic import ValidationError

from server.memory.api.models_crud import (
    MemorySetInput,
    MemoryUpdateByIdInput,
    MemoryUpdateInput,
)

_IDENTITY = {"org": "o", "namespace": "ns", "agent": "a"}


def _inputs(relation):
    yield MemorySetInput(**_IDENTITY, memory_key="k", value="v", relations=[relation])
    yield MemoryUpdateInput(**_IDENTITY, memory_key="k", value="v", relations=[relation])
    yield MemoryUpdateByIdInput(org="o", memory_id="m", value="v", relations=[relation])


@pytest.mark.parametrize("subject", ["I", "me", "Alice"])
def test_a_subject_that_folds_to_a_name_is_accepted(subject):
    for model in _inputs((subject, "lives_in", "paris")):
        assert model.relations == [(subject, "lives_in", "paris")]


@pytest.mark.parametrize(
    "relation",
    [("  ", "lives_in", "paris"), ("user", "lives_in", "  "), ("user", "  ", "paris")],
)
def test_a_part_that_is_blank_once_canonical_is_refused(relation):
    for build in (MemorySetInput, MemoryUpdateInput):
        with pytest.raises(ValidationError):
            build(**_IDENTITY, memory_key="k", value="v", relations=[relation])
    with pytest.raises(ValidationError):
        MemoryUpdateByIdInput(org="o", memory_id="m", value="v", relations=[relation])


def _refusal(relation) -> str:
    with pytest.raises(ValidationError) as caught:
        MemorySetInput(**_IDENTITY, memory_key="k", value="v", relations=[relation])
    return caught.value.errors()[0]["msg"]


@pytest.mark.parametrize(("verb", "stored"), [("has", "''"), ("is a", "'a'")])
def test_a_refused_relation_names_what_it_was_stored_as(verb, stored):
    message = _refusal(("Alice", verb, "cat"))
    assert f"{verb!r} is stored as {stored}" in message
    assert "copulas" in message and "punctuation" in message
    assert "content verb such as 'owns'" in message


@pytest.mark.parametrize(
    ("verb", "stored"),
    [
        ("владеет", "владеет"),
        ("ВЛАДЕЕТ", "владеет"),
        ("所有", "所有"),
        ("좋아한다", "좋아한다"),
        ("रखता है", "रखता_है"),
        # The stemmer cuts the final e, as it cuts "owns" to "own"; the è stays.
        ("possède", "possèd"),
        ("posse\u0300de", "possèd"),  # the same word typed as e and a combining accent
    ],
)
def test_a_relation_in_any_script_keeps_its_letters(verb, stored):
    from server.memory.api.models_crud import _relation_verb
    from server.memory.domain.graph_quality import canonical_relation
    from server.memory.domain.search_text import canonicalize_verb

    for model in _inputs(("Ana", verb, "car")):
        assert model.relations == [("Ana", verb, "car")]
    assert _relation_verb(verb) == stored
    # An extracted relation is canonicalised once when parsed and again when written:
    # it lands on the same predicate as a point write's.
    assert canonical_relation(canonicalize_verb(canonicalize_verb(verb))).verb == stored


@pytest.mark.parametrize(
    ("verb", "says"),
    [
        # Only too short: nothing was dropped, so the refusal names no drop.
        ("x", "'x' is too short: a relation needs at least 2 characters"),
        # Punctuation is dropped, and named as such.
        ("!!", "'!!' is stored as '' (copulas, auxiliaries and punctuation are dropped)"),
        ("é!", "'é!' is stored as 'é' (copulas, auxiliaries and punctuation are dropped)"),
    ],
)
def test_a_refusal_names_a_drop_only_when_there_was_one(verb, says):
    message = _refusal(("Alice", verb, "cat"))
    assert says in message
    assert ("dropped" in message) is (verb != "x")
    assert "non-Latin" not in message


def test_a_subject_that_is_only_too_short_says_so():
    message = _refusal(("x", "owns", "cat"))
    assert message.endswith(
        "'x' is too short: a subject needs at least 2 characters, such as a name"
    )


def test_a_refused_subject_names_what_it_was_stored_as():
    message = _refusal(("the ?", "owns", "cat"))
    assert "'the ?' is stored as ''" in message
    assert "articles, possessives and punctuation" in message
    assert "a subject needs at least 2 characters" in message


def test_a_part_longer_than_the_cap_once_normalised_says_so():
    long = "\ufb01" * 200  # 400 characters once normalised
    for fact in ((long, "owns", "cat"), ("bob", "owns", long)):
        assert "is longer than 256 characters once normalised" in _refusal(fact)


def test_a_statuses_id_is_at_most_64_characters():
    from server.memory.api.models_crud import MemoryStatusesInput

    assert MemoryStatusesInput(org="o", namespace="ns", memory_ids=["a" * 64])
    with pytest.raises(ValidationError):
        MemoryStatusesInput(org="o", namespace="ns", memory_ids=["a" * 65])
