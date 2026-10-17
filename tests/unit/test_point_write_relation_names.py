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


@pytest.mark.parametrize(("verb", "stored"), [("has", "''"), ("is a", "'a'"), ("좋아한다", "''")])
def test_a_refused_relation_names_what_it_was_stored_as(verb, stored):
    message = _refusal(("Alice", verb, "cat"))
    assert f"{verb!r} is stored as {stored}" in message
    assert "copulas" in message and "non-Latin letters" in message
    assert "content verb such as 'owns'" in message


def test_a_refused_subject_names_what_it_was_stored_as():
    message = _refusal(("the ?", "owns", "cat"))
    assert "'the ?' is stored as ''" in message
    assert "articles, possessives and punctuation" in message
    assert "a subject needs at least 2 characters" in message
