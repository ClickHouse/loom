"""A question that reads time gets the timeline block and the
raw turns of its top rows under one switch; the cue helper is LLM-free and
the switch is off after a null measurement."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.app.ask import question_shapes


def test_looks_temporal_cues() -> None:
    assert question_shapes.looks_temporal("When did Caroline go to the LGBTQ conference?")
    assert question_shapes.looks_temporal("How long ago did Melanie paint the sunrise?")
    assert not question_shapes.looks_temporal("What is Caroline's dog called?")
    assert not question_shapes.looks_temporal("")
    # an absolute date is the grammar's job: it arrives as a resolved date
    assert not question_shapes.looks_temporal("What did Jon do in March 2023?")
    assert question_shapes.looks_temporal(
        "What did Jon do in March 2023?", SimpleNamespace(resolved_dates=("2023-03-01",))
    )
    assert question_shapes.looks_temporal("list them", None, "chronological")
    assert not question_shapes.looks_temporal(
        "list them", SimpleNamespace(resolved_dates=()), "relevance"
    )


def test_setting_defaults_after_the_dated_turns_measurement() -> None:
    """On, with the LLM selector: "top" measured null twice (once on correctly
    dated turns), while "llm" chose the turns that mattered (130 → 136 / 161,
    9 fixed / 3 lost)."""
    from server.settings import Settings

    assert Settings().memory_ask_temporal_deep_select == "llm"
