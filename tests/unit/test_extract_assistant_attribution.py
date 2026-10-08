"""Speaker attribution from the evidence: an item only the
assistant named is the assistant's recommendation, however the extractor
phrased it: "I'm all about Aged Cheddar" said by the assistant must not
become "User is interested in Aged Cheddar"."""

from __future__ import annotations

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.speakers import _candidate_items, attribute_assistant_introduced

MSGS = [
    {"role": "user", "content": "I love a good legal drama. Any book ideas?"},
    {
        "role": "assistant",
        "content": "I'm really into The Cases That Haunt Us; it's such a fascinating read!",
    },
    {"role": "user", "content": "Nice. I also enjoyed A Civil Action last year."},
    {
        "role": "assistant",
        "content": "I'm all about Aged Cheddar; it's such a delicious choice that anyone would enjoy!",
    },
    {
        "role": "assistant",
        "content": "I really think Good Will Hunting from 1997 is a fantastic movie.",
    },
]


def test_candidate_items_pick_title_spans() -> None:
    items = _candidate_items(
        "User is interested in the book The Cases That Haunt Us, which delves into cases."
    )
    assert "Cases That Haunt Us" in items or "The Cases That Haunt Us" in items
    assert _candidate_items("User loves cooking.") == []
    assert (
        "Good Will Hunting"
        in _candidate_items("User wants to watch Good Will Hunting (1997) soon.")[0]
    )


def test_assistant_only_items_are_reattributed() -> None:
    facts = [
        ExtractedFact(
            memory_key="user_book_cases_that_haunt_us",
            value="User is interested in the book The Cases That Haunt Us, which delves into criminal cases.",
            tags=["reading"],
        ),
        ExtractedFact(
            memory_key="user_cheese_aged_cheddar", value="User's favourite cheese is Aged Cheddar."
        ),
        # the user named this one → stays the user's
        ExtractedFact(
            memory_key="user_book_a_civil_action", value="User enjoyed A Civil Action last year."
        ),
        # already attributed → untouched
        ExtractedFact(
            memory_key="assistant_recommended_good_will_hunting",
            value="Assistant recommended Good Will Hunting (1997).",
        ),
        # no title-cased item → untouched
        ExtractedFact(memory_key="user_likes_legal_drama", value="User loves a good legal drama."),
    ]
    out = attribute_assistant_introduced(facts, MSGS)
    by = {f.memory_key: f for f in out}
    assert len(out) == 5
    book = by["assistant_book_cases_that_haunt_us"]
    assert book.value.startswith("Assistant recommended") and "Cases That Haunt Us" in book.value
    assert "The user is interested in the book" in book.value
    assert "speaker:assistant" in book.tags and "reading" in book.tags
    cheese = by["assistant_cheese_aged_cheddar"]
    assert cheese.value.startswith(
        "Assistant recommended Aged Cheddar. The user's favourite cheese"
    )
    assert by["user_book_a_civil_action"].value == "User enjoyed A Civil Action last year."
    assert by["assistant_recommended_good_will_hunting"].tags == []
    assert by["user_likes_legal_drama"].value == "User loves a good legal drama."


def test_no_assistant_turns_is_a_no_op() -> None:
    facts = [ExtractedFact(memory_key="user_x", value="User likes The Godfather Part II.")]
    assert (
        attribute_assistant_introduced(
            facts, [{"role": "user", "content": "I like The Godfather Part II."}]
        )
        == facts
    )
    assert attribute_assistant_introduced([], MSGS) == []
