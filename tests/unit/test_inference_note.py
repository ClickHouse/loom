"""An inference question is answered as an inference."""

from __future__ import annotations

import inspect

from server.memory.app.ask.prompts import _INFERENCE_NOTE
from server.memory.app.ask.question_shapes import is_inference_question
from server.memory.app.ask.synthesis import _synthesize


def test_inference_questions_are_detected_and_fact_questions_are_not() -> None:
    for q in (
        "Would Melanie go on another roadtrip soon?",
        "Would Caroline be considered religious?",
        "What job might Maria pursue in the future?",
        "Would John be open to moving to another country?",
        "Is Melanie likely to keep running?",
        "Could Caroline have pursued counseling without support growing up?",
        "What might John's financial status be?",
    ):
        assert is_inference_question(q), q
    for q in (
        "What is Melanie's dog's name?",
        "When did Melanie run a charity race?",
        "Where has Melanie camped?",
        "Which country does the current CEO of Twitter hold citizenship of?",
        "Is Oscar Melanie's pet?",
    ):
        assert not is_inference_question(q), q


def test_note_exists_and_is_threaded() -> None:
    assert "INFERENCE" in _INFERENCE_NOTE and "Likely no" in _INFERENCE_NOTE
    assert "inference_note" in inspect.signature(_synthesize).parameters
