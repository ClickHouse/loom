"""A when-question's reader derives from relative phrases."""

from __future__ import annotations

import inspect

from server.memory.app.ask.prompts import _TEMPORAL_DERIVE_NOTE
from server.memory.app.ask.question_shapes import is_when_question
from server.memory.app.ask.synthesis import _synthesize


def test_when_questions_are_detected() -> None:
    for q in (
        "When did John get his dog Max?",
        "When did John go on a camping trip with Max?",
        "How long has Melanie been practicing art?",
        "How many weeks passed between Maria adopting Coco and Shadow?",
        "In what year did Caroline move?",
        "What year did Melanie read the book?",
        "How old is Xu Jia?",
    ):
        assert is_when_question(q), q
    for q in (
        "What is Melanie's dog's name?",
        "Where has Melanie camped?",
        "Would Melanie go on another roadtrip soon?",
        "Which country does the current CEO of Twitter hold citizenship of?",
    ):
        assert not is_when_question(q), q


def test_note_exists_and_is_threaded() -> None:
    assert "WHEN question" in _TEMPORAL_DERIVE_NOTE and "approximate" in _TEMPORAL_DERIVE_NOTE
    assert "temporal_derive" in inspect.signature(_synthesize).parameters
