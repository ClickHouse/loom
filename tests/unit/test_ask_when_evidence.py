"""The when-question fallback's pure halves and its recall call."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from server.memory.app import ask_when_evidence as awe


def test_when_questions_are_recognised_and_others_are_not() -> None:
    yes = [
        "When did Deborah start the yoga class?",
        "What day did Tim get into his study abroad program?",
        "Which month did John achieve a career-high?",
        "How long did it take for Joanna to finish her book?",
        "How many weeks passed between Maria adopting Coco and Shadow?",
        "In which year did Nate get his turtles?",
    ]
    no = [
        "What movie did Joanna watch?",
        "Who did Maria have dinner with?",
        "Does Melanie like camping?",
    ]
    assert all(awe.is_when_question(q) for q in yes)
    assert not any(awe.is_when_question(q) for q in no)


def test_not_recorded_answers_are_recognised() -> None:
    assert awe.looks_not_recorded("The memories do not record Melanie reading that book.")
    assert awe.looks_not_recorded("There is no memory of a pride festival together.")
    assert awe.looks_not_recorded("The memories don't have a record of the date.")
    assert not awe.looks_not_recorded("Melanie read it in 2022 [#book].")
    # phrasings seen on multi-session misses
    for text in (
        "The memories don't record your starting count for that two-week period.",
        "The exact increase isn't specified.",
        "No cashback amount is recorded for that specific trip.",
        "No specific date is stated [user_nlp_background].",
        "No price is recorded for your luxury boots, so the difference can't be calculated.",
        "The memories do not give the actual submission date.",
        "The memories state this as a total count, not an explicit increase from a start.",
        "edX online courses — unclear: no count stated.",
    ):
        assert awe.looks_not_recorded(text), text
    assert not awe.looks_not_recorded("The difference is $750 [user_boots_purchase].")


def test_event_phrase_and_tokens_drop_the_interrogative_head() -> None:
    assert (
        awe.event_phrase("When did Deborah start the yoga class?") == "Deborah start the yoga class"
    )
    assert awe.phrase_tokens("When did Deborah start the yoga class?") == [
        "deborah",
        "yoga",
        "class",
    ]
    assert awe.phrase_tokens("How long did it take for Joanna to finish writing her book?") == [
        "joanna",
        "finish",
        "writing",
        "book",
    ]


def test_render_turns_dates_each_turn_and_trims() -> None:
    turns = [
        awe.DatedTurn(
            day="2023-03-13",
            role="user",
            content="Deborah: I started a yoga class last Friday!  " + "x" * 400,
        ),
        awe.DatedTurn(day="", role="assistant", content="Nice."),
    ]
    text = awe.render_turns(turns, max_chars=60)
    lines = text.splitlines()
    assert lines[0].startswith("[turn @ 2023-03-13] user: Deborah: I started a yoga class")
    assert lines[0].endswith("…") and len(lines[0]) < 110
    assert lines[1] == "[turn @ date unknown] assistant: Nice."
    assert "resolve any relative phrase" in awe.WHEN_EVIDENCE_NOTE


@pytest.mark.asyncio
async def test_fetch_dated_turns_recalls_by_any_token_and_fails_open(monkeypatch) -> None:
    import server.memory.app.recall as rh

    seen = {}

    async def fake_recall(inp):
        seen["inp"] = inp
        return SimpleNamespace(
            turns=[
                SimpleNamespace(
                    role="user",
                    observed_at="2023-03-13T16:33",
                    content="Deborah: I started yoga last Friday",
                ),
                SimpleNamespace(role="user", observed_at="", content=""),
            ]
        )

    monkeypatch.setattr(rh, "handle_memory_recall", fake_recall)
    turns = await awe.fetch_dated_turns(
        org="dev",
        namespace="ns",
        agent="bot",
        user_id=None,
        question="When did Deborah start the yoga class?",
        top_k=6,
    )
    assert [t.day for t in turns] == ["2023-03-13"] and turns[0].content.startswith("Deborah")
    inp = seen["inp"]
    assert inp.match == "any" and inp.order == "relevance" and inp.top_k == 6
    assert inp.query == "deborah yoga class" and inp.namespace == "ns"

    async def boom(inp):
        raise RuntimeError("down")

    monkeypatch.setattr(rh, "handle_memory_recall", boom)
    assert (
        await awe.fetch_dated_turns(
            org="dev", namespace="ns", agent="bot", user_id=None, question="When did X?", top_k=3
        )
        == []
    )
    assert (
        await awe.fetch_dated_turns(
            org="dev", namespace="ns", agent="bot", user_id=None, question="When?", top_k=3
        )
        == []
    )


@pytest.mark.asyncio
async def test_source_evidence_rows_take_the_message_day_then_the_observation_date(
    monkeypatch,
) -> None:
    """A replayed turn is evidence of the day it was said."""
    import server.memory.app.extract.evidence as ev

    captured = []

    async def fake_write(items):
        captured.extend(items)
        return [SimpleNamespace(evidence_id=f"e{i}") for i, _ in enumerate(items)]

    monkeypatch.setattr("server.memory.app.evidence.write_evidence_rows", fake_write)
    monkeypatch.setattr(ev, "messages_source_resource_id", lambda inp: "")
    inp = SimpleNamespace(
        org="dev", namespace="ns", agent="bot", user_id="-", session_id="s1", run_id="-"
    )
    msgs = [
        {"role": "user", "content": "stamped", "timestamp": "2023-03-13T16:33"},
        {"role": "user", "content": "unstamped"},
    ]
    ids = await ev._write_source_evidence(inp, msgs, observation_date="2023-03-13")
    assert ids == ["e0", "e1"]
    assert captured[0].observed_at == datetime(2023, 3, 13, 16, 33)
    assert captured[1].observed_at == datetime(2023, 3, 13)
    captured.clear()
    await ev._write_source_evidence(inp, [{"role": "user", "content": "no date at all"}])
    assert captured[0].observed_at is None  # the writer falls back to now


def test_advice_requests_are_detected_and_facts_are_not() -> None:
    from server.memory.app.ask_when_evidence import is_advice_request

    for q in (
        "I've been having trouble with the battery life on my phone lately. Any tips?",
        "Can you suggest a hotel for my upcoming trip to Miami?",
        "I'm trying to decide whether to buy a NAS device now or wait. What do you think?",
        "I've been sneezing quite a bit lately. Do you think it might be my living room?",
    ):
        assert is_advice_request(q), q
    for q in (
        "How much did the luxury boots cost?",
        "When did I order the couch?",
        "How many weddings did I attend?",
    ):
        assert not is_advice_request(q), q


def test_cue_window_bounds_cover_the_named_days_and_nothing_wide() -> None:
    from server.memory.app.ask_when_evidence import cue_window_bounds

    b = cue_window_bounds("What did I cook for my friend a couple of days ago?", "2022-04-12")
    assert b is not None and b[0] <= "2022-04-09" and b[1] >= "2022-04-11"
    assert cue_window_bounds("Which bike did I service the past weekend?", "2023-03-21") is not None
    assert cue_window_bounds("What did I do this year?", "2023-03-21") is None
    assert cue_window_bounds("What is my dog called?", "2023-03-21") is None
    assert cue_window_bounds("a week ago?", None) is None


async def test_window_turns_join_the_phrase_turns_when_the_setting_is_on(monkeypatch) -> None:
    import server.memory.app.ask_when_evidence as awe

    calls = []

    async def fake_recall(kw):
        calls.append(kw)
        if "since" not in kw:
            return [awe.DatedTurn(day="2022-04-09", role="user", content="I made beef stew.")]
        return [
            awe.DatedTurn(
                day="2022-04-10", role="user", content="I baked a chocolate cake for my friend."
            ),
            awe.DatedTurn(day="2022-04-09", role="user", content="I made beef stew."),
        ]

    q = "I mentioned cooking something for my friend a couple of days ago. What was it?"
    monkeypatch.setattr(awe, "_recall_turns", fake_recall)
    turns = await awe.fetch_dated_turns(
        org="o",
        namespace="n",
        agent="a",
        user_id=None,
        question=q,
        top_k=6,
        observation_date="2022-04-12",
    )
    assert [t.content for t in turns] == [
        "I made beef stew.",
        "I baked a chocolate cake for my friend.",
    ]
    assert calls[1]["role"] == "user" and calls[1]["match"] == "any" and calls[1]["since"]


async def test_order_window_turns_join_the_first_reading_for_ordering_questions(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    import server.memory.app.ask_when_evidence as awe
    from server.memory.app.ask.retrieval_legs import _order_window_turns

    async def fake_recall(kw):
        assert kw["role"] == "user" and "since" in kw
        return [
            awe.DatedTurn(
                day="2023-01-08",
                role="user",
                content="I went to an NBA game at the Staples Center!",
            )
        ]

    monkeypatch.setattr(awe, "_recall_turns", fake_recall)
    st = SimpleNamespace(
        memory_ask_order_window_max_days=95,
        memory_ask_cue_window_turns_top_k=24,
    )
    si = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id=None, observation_date="2023-01-30"
    )
    text, note = await _order_window_turns(
        si, "What is the order of the sports events I watched in January?", st
    )
    assert "Staples Center" in text and note == awe.ORDER_TURNS_NOTE
    assert await _order_window_turns(si, "How many sports events did I watch in January?", st) == (
        "",
        "",
    )
    assert await _order_window_turns(
        si, "What is the order of the sports events I watched?", st
    ) == ("", "")


def test_period_bounds_read_months_and_past_spans() -> None:
    from datetime import date

    from server.memory.app.ask_when_evidence import cue_window_bounds, period_bounds

    assert period_bounds("the sports events I watched in January?", date(2023, 1, 30)) == (
        date(2023, 1, 1),
        date(2023, 1, 31),
    )
    assert period_bounds("what I did in December", date(2023, 1, 30)) == (
        date(2022, 12, 1),
        date(2022, 12, 31),
    )
    assert period_bounds("concerts I attended in the past two months", date(2023, 4, 22)) == (
        date(2023, 2, 21),
        date(2023, 4, 22),
    )
    assert period_bounds("trips in the past three months", date(2023, 6, 1))[0] == date(2023, 3, 3)
    assert period_bounds("what is my dog called", date(2023, 6, 1)) is None
    assert (
        cue_window_bounds(
            "the concerts I attended in the past two months", "2023-04-22", max_days=95
        )
        is not None
    )
    assert cue_window_bounds("the concerts I attended in the past two months", "2023-04-22") is None
