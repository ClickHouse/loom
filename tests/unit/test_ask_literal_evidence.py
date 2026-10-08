"""Literal completion: the raw turns that name the asked value join the count."""

from __future__ import annotations

import asyncio

from server.memory.app import ask_literal_evidence as le


def test_question_literals_places_quotes_and_nothing_for_abstract_questions() -> None:
    assert le.question_literals("How many events are happening in Chicago?") == ["Chicago"]
    assert le.question_literals("How many people do I know who work in Las Vegas, NV?") == [
        "Las Vegas"
    ]
    assert le.question_literals('How many people work at "Acme Corp"?') == ["Acme Corp"]
    assert (
        le.question_literals("How many people are celebrating their birthdays in the summer?") == []
    )
    # Only the first line (the stem) is read; the option block never contributes.
    assert le.question_literals("How many events in Houston?\nA) 9 events\nB) Denver") == [
        "Houston"
    ]


def test_turn_hits_dedupe_text_and_keys() -> None:
    rows = [
        {
            "session_id": "s0",
            "role": "user",
            "observed_at": "2024-10-01 08:10:00",
            "content": "CraftFit Fest is happening in Chicago, IL.",
        },
        {
            "session_id": "s0",
            "role": "user",
            "observed_at": "2024-10-01 08:10:00",
            "content": "Craft Fest is happening in Chicago, IL.",
        },
        {
            "session_id": "s0",
            "role": "user",
            "observed_at": "2024-10-01 08:11:00",
            "content": "craftfit fest is happening in chicago, il.",
        },
    ]

    class _R:
        memory_key = "turn:s0:08:10"

    hits = le.turn_hits(rows, [_R()], max_hits=10)
    assert [h.memory_key for h in hits] == ["turn:s0:08:10#2", "turn:s0:08:10#3"]
    assert hits[0].content_excerpt.startswith("[user @ 2024-10-01 08:10] CraftFit Fest")
    assert hits[0].source == "evidence"


def test_literal_complete_reads_each_literal_and_fails_open(monkeypatch) -> None:
    class _SI:
        org = "o"
        namespace = "n"

    calls: list[str] = []

    async def _fetch(**kw):
        calls.append(kw["literal"])
        return [
            {
                "session_id": "s0",
                "role": "user",
                "observed_at": "2024-10-01 08:10:00",
                "content": f"X is in {kw['literal']}",
            }
        ]

    monkeypatch.setattr(le, "fetch_turns_with_literal", _fetch)
    out = asyncio.run(
        le.literal_complete(_SI(), "How many events are happening in Chicago?", [], max_hits=40)
    )
    assert calls == ["Chicago"] and len(out) == 1
    assert (
        asyncio.run(le.literal_complete(_SI(), "How many birthdays in summer?", [], max_hits=40))
        == []
    )

    async def _boom(**kw):
        raise RuntimeError("ch down")

    monkeypatch.setattr(le, "fetch_turns_with_literal", _boom)
    assert (
        asyncio.run(le.literal_complete(_SI(), "How many events in Chicago?", [], max_hits=40))
        == []
    )
