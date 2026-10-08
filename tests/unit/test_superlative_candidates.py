"""An amount superlative is ranked from (entity, amount) pairs
read off the pool rows."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.app.ask import prompts, question_shapes


def _m(key, text, when="2023-05-20"):
    return SimpleNamespace(memory_key=key, content_excerpt=text, valid_at=when)


POOL = [
    _m(
        "user_grocery_shopping",
        '"User went grocery shopping on 2023-05-20 and spent around $120 at Walmart, which is higher than expected."',
    ),
    _m(
        "user_sustainable_shopping",
        '"User placed an online order with Thrive Market last month and spent around $150 on organic products."',
        "2023-04-26",
    ),
    _m(
        "user_trader_joes",
        '"User and their sister spent around $80 at Trader Joe\'s the week before last on snacks."',
        "2023-05-15",
    ),
    _m(
        "user_publix",
        '"User ordered from Publix and spent around $60, with a $10 delivery fee."',
        "2023-05-23",
    ),
    _m(
        "user_followers",
        '"User gained 200 followers on Instagram over the past three weeks."',
        "2023-05-28",
    ),
]


def test_shape_gate() -> None:
    assert question_shapes.is_amount_superlative_question(
        "Which grocery store did I spend the most money at in the past month?"
    )
    assert question_shapes.is_amount_superlative_question(
        "Which platform did I gain the most followers on?"
    )
    assert not question_shapes.is_amount_superlative_question(
        "What is the most common cause of the flaky test?"
    )
    assert not question_shapes.is_amount_superlative_question("How much did I spend at Walmart?")


def test_candidates_are_entity_amount_pairs_largest_first() -> None:
    c = prompts.amount_candidates(POOL, money=True)
    top = [(e, a) for e, a, _k, _w in c]
    assert top[0] == ("Thrive Market", 150.0)
    assert ("Walmart", 120.0) in top and ("Trader Joe's", 80.0) in top and ("Publix", 60.0) in top
    assert ("Instagram", 200.0) not in top  # a follower count is not money
    assert c[0][2] == "user_sustainable_shopping" and c[0][3] == "2023-04-26"
    assert [(e, a) for e, a, _k, _w in prompts.amount_candidates(POOL, money=False)] == [
        ("Instagram", 200.0)
    ]


def test_note_only_with_two_or_more_candidates() -> None:
    q = "Which grocery store did I spend the most money at in the past month?"
    notes = prompts._question_notes(q, POOL)
    assert (
        len(notes) == 1
        and "Thrive Market — 150 (2023-04-26) [user_sustainable_shopping]" in notes[0]
    )
    assert notes[0].index("Thrive Market") < notes[0].index("Walmart")
    assert prompts._question_notes(q, POOL[:1]) == []  # one candidate: no note
    assert prompts._question_notes("How many years did I study?", POOL) == []
