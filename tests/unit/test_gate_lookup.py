"""A mention-time lookup or a how-many question is not silenced by the gate."""

from __future__ import annotations

from server.memory.app.ask.question_shapes import _HOW_MANY_Q_RE, is_mention_time_question


def test_the_two_silenced_shapes_are_recognised() -> None:
    assert is_mention_time_question(
        "I mentioned an investment for a competition four weeks ago? What did I buy?"
    )
    assert _HOW_MANY_Q_RE.search("How many sports have I played competitively in the past?")
    assert not _HOW_MANY_Q_RE.search("What is my sister's name?")
