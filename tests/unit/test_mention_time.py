"""A mention-time question reads the transcript clock."""

from __future__ import annotations

from server.memory.app.ask.question_shapes import is_mention_time_question


def test_mention_verb_plus_relative_time_triggers() -> None:
    assert is_mention_time_question(
        "I mentioned cooking something for my friend a couple of days ago. What was it?"
    )
    assert is_mention_time_question("We talked about a movie last week — which one?")


def test_event_questions_do_not_trigger() -> None:
    # event-time questions keep the event clock
    assert not is_mention_time_question("What did I cook for my friend?")
    assert not is_mention_time_question("When did I bake the chocolate cake?")
    # a mention verb without a relative time is not a mention-time question
    assert not is_mention_time_question("What have I mentioned about my sister?")
