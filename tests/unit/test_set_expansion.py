"""Set-question instance expansion."""

from __future__ import annotations

import pytest

from server.memory.domain import set_expansion as se

POSITIVE = [
    "What martial arts has John done?",
    "What desserts has Maria made?",
    "What states has Maria vacationed at?",
    "What items has Melanie bought?",
    "What board games has Nate played?",
    "What books has Melanie read?",
    "Which cities have both Jean and John visited?",
    "What animal do both Nate and Joanna like?",
    "What movies have both Joanna and Nate seen?",
    "What do Jon and Gina have in common?",
    "How many tournaments has Nate won?",
    "How many children does Melanie have?",
    "What kinds of food does Maria cook?",
    "What events for veterans has John participated in?",
    "Who have written notes of gratitude to Maria?",
]
NEGATIVE = [
    "What is Caroline's identity?",
    "Where did Caroline move from 4 years ago?",
    "When did Melanie paint a sunrise?",
    "How long did it take for Jon to open his studio?",
    "Why did Gina decide to start her own clothing store?",
    "Who is Nate's best friend?",
    "What is Caroline's relationship status?",
    "How old is Melanie?",
]


@pytest.mark.parametrize("q", POSITIVE)
def test_set_questions_fire(q: str) -> None:
    assert se.is_set_question(q), q


@pytest.mark.parametrize("q", NEGATIVE)
def test_single_answer_questions_do_not_fire(q: str) -> None:
    assert not se.is_set_question(q), q


def test_question_entities() -> None:
    assert se.question_entities("Which cities have both Jean and John visited?") == ["Jean", "John"]
    assert se.question_entities("What did Caroline research?") == ["Caroline"]
    assert se.question_entities("how many?") == []
