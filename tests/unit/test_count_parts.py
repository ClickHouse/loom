"""A count question is answered from its parts."""

from __future__ import annotations

from server.memory.app.ask.question_shapes import (
    _count_parts_in_pool,
    is_span_total_question,
    is_total_number_question,
)
from server.memory.domain import set_expansion as se


class _Hit:
    def __init__(self, key: str, text: str, distance: float = 0.5) -> None:
        self.memory_key = key
        self.content_excerpt = text
        self.distance = distance


def test_total_number_of_is_a_set_and_count_question() -> None:
    q = "What is the total number of siblings I have?"
    assert se.is_set_question(q)
    assert is_total_number_question(q)
    assert not is_total_number_question("What is my sister's name?")


def test_collective_nouns_expand_to_their_instance_words() -> None:
    assert se.collective_phrases("What is the total number of siblings I have?") == [
        "has a sister",
        "has a brother",
    ]
    assert "dog" in se.collective_terms("How many pets do I have?")
    assert se.collective_phrases("Where did I go last summer?") == []


def test_count_parts_in_pool_needs_a_part_row() -> None:
    q = "What is the total number of siblings I have?"
    pool = [
        _Hit("number_solution", "The number is 12, as it satisfies the equation."),
        _Hit("user_family_background", "User comes from a family with 3 sisters."),
    ]
    assert _count_parts_in_pool(q, pool)
    assert not _count_parts_in_pool(q, [_Hit("number_solution", "The number is 12.")])
    # a plain head noun works without a collective table entry
    assert _count_parts_in_pool(
        "How many books have I read?", [_Hit("k", "User read the book Dune.")]
    )
    assert not _count_parts_in_pool("What is my sister's name?", pool)


def test_span_total_questions() -> None:
    assert is_span_total_question(
        "How many years in total did I spend in formal education from high school "
        "to the completion of my Bachelor's degree?"
    )
    assert not is_span_total_question("How many books did I read?")
    assert not is_span_total_question("What is the total number of siblings I have?")
