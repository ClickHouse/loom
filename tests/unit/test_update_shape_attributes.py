"""An update to one attribute shares its subject AND its attribute: two
statements that share only a bare name before the copula are two attributes
of one person, and the newer must not retire the older (an age replaced by an
occupation, a nickname, a mentor)."""

from __future__ import annotations

import pytest

from server.memory.domain.conflicts import intra_batch_supersessions, templated_substitution
from server.memory.domain.triples_follow_rows import object_substitution


def _is_update(old: str, new: str) -> bool:
    return templated_substitution(old, new) and bool(object_substitution(old, new))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Wang Xiaoming is 28 years old.", "Wang Xiaoming is a software development engineer."),
        ("Liang Xin is 32 years old.", "Liang Xin is affectionately known as Xinxin."),
        ("Wang Xiaoming's nationality: China", "Wang Xiaoming's ethnic background: Asia"),
    ],
)
def test_two_attributes_of_one_subject_are_not_an_update(old: str, new: str) -> None:
    assert not _is_update(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("The capital of Italy is Rome.", "The capital of Italy is Duluth."),
        ("The chairperson of Fatah is Mahmoud Abbas.", "The chairperson of Fatah is Moshe Kahlon."),
        ("Amala Paul is a citizen of India.", "Amala Paul is a citizen of Belgium."),
        ("Wang Xiaoming is 28 years old.", "Wang Xiaoming is 29 years old."),
        (
            "basketball was created in the country of United States of America.",
            "basketball was created in the country of Soviet Union.",
        ),
    ],
)
def test_one_attribute_with_a_new_value_is_an_update(old: str, new: str) -> None:
    assert _is_update(old, new)


@pytest.mark.parametrize(
    ("old", "new", "objects"),
    [
        ("Italy’s capital is Rome.", "Italy’s capital is Milan.", ("rome", "milan")),
        ("Italy's capital is Rome.", "Italy's capital is Milan.", ("rome", "milan")),
        ("Alice’s manager is Bob.", "Alice’s manager is Carol.", ("bob", "carol")),
        ("Alice's manager is Bob.", "Alice's manager is Carol.", ("bob", "carol")),
    ],
)
def test_a_possessive_names_the_attribute(old: str, new: str, objects: tuple[str, str]) -> None:
    # "Italy's" is one word, curly or straight; the possessive makes the phrase
    # before the copula an attribute, like "the capital of Italy".
    assert object_substitution(old, new) == objects
    assert _is_update(old, new)


@pytest.mark.parametrize("name", ["Macy's", "McDonald's", "Wendy's", "Kellogg's", "Macy’s"])
def test_a_brand_name_ending_in_s_is_not_a_possessive(name: str) -> None:
    # The name is the last word before the copula: no noun follows it, so it
    # names a thing, and two opinions of it are two facts.
    old, new = f"User thinks {name} is expensive.", f"User thinks {name} is crowded."
    assert not _is_update(old, new)
    assert intra_batch_supersessions([old, new]) == []


def test_a_possessive_update_in_one_batch_retires_the_earlier_row() -> None:
    values = ["Italy’s capital is Rome.", "Italy’s capital is Milan."]
    assert intra_batch_supersessions(values) == [(0, 1)]
