"""A calibration abstain does not silence arithmetic over dated rows."""

from __future__ import annotations

import pytest

from server.memory.app.ask.question_shapes import is_arithmetic_question


@pytest.mark.parametrize(
    "q",
    [
        "How many days had passed between the day I bought a gift for my brother's "
        "graduation ceremony and the day I bought a birthday gift for my best friend?",
        "How many days passed between the day I replaced my spark plugs and the day I "
        "participated in the Turbocharged Tuesdays auto racking event?",
        "Who did I meet first, Mark and Sarah or Tom?",
        "Which came first, my trip to Lisbon or the job interview?",
        "How long after moving to Austin did I adopt the dog?",
        "How many weeks apart were my two dentist appointments?",
        "Did I start the pottery class before or after the marathon?",
        "How much time passed between my two visits to the clinic?",
    ],
)
def test_date_difference_and_ordering_questions_are_arithmetic(q: str) -> None:
    assert is_arithmetic_question(q)


@pytest.mark.parametrize(
    "q",
    [
        "How many antique items did I inherit from my family members?",
        "What is my sister's name?",
        "Which grocery store did I spend the most money at last month?",
        "What did I cook for my friend a couple of days ago?",
        "When did I last visit Paris?",
        "How many siblings do I have?",
        "",
    ],
)
def test_counts_superlatives_and_lookups_are_not_arithmetic(q: str) -> None:
    assert not is_arithmetic_question(q)
