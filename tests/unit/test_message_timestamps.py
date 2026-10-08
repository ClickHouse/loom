"""Per-message timestamps on memory.set_from_messages.

MemBench Emotion asks how the user felt "around 08:10" inside a session
whose turns are a minute apart; with only the session date the reader
cannot tell which cue is current. A message may say when it was said: the evidence row's observed_at is that moment, the extractor sees
`[role @ YYYY-MM-DD HH:MM]`, and a cited fact left unanchored (or anchored
to the day) takes the turn's time. CH-free.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from server.memory.api.models_bulk import MemorySetFromMessagesInput, _Message
from server.memory.app.ask.rendering import _recorded_date
from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.dates import apply_message_timestamps
from server.memory.app.extract.handler import _message_dict
from server.memory.app.extract.turns import _format_messages


def test_message_timestamp_parses_and_rides_as_minutes() -> None:
    m = _Message(role="user", content="hi", timestamp="2024-10-01T08:10:00")
    assert m.timestamp == datetime(2024, 10, 1, 8, 10)
    assert _message_dict(m) == {"role": "user", "content": "hi", "timestamp": "2024-10-01T08:10"}
    assert _message_dict(_Message(role="user", content="hi")) == {"role": "user", "content": "hi"}
    inp = MemorySetFromMessagesInput(
        org="o",
        namespace="n",
        agent="a",
        messages=[{"role": "user", "content": "x", "timestamp": "2024-10-01 08:10"}],
    )
    assert inp.messages[0].timestamp == datetime(2024, 10, 1, 8, 10)


def test_format_messages_shows_the_turn_time() -> None:
    msgs = [
        {"role": "user", "content": "I felt belittled.", "timestamp": "2024-10-01T08:10"},
        {"role": "assistant", "content": "That sounds hard."},
    ]
    out = _format_messages(msgs)
    assert out.splitlines()[0] == "[user @ 2024-10-01 08:10] I felt belittled."
    assert out.splitlines()[1] == "[assistant] That sounds hard."
    assert (
        _format_messages(msgs, include_indices=True)
        .splitlines()[0]
        .startswith("[0:user @ 2024-10-01 08:10]")
    )
    # a malformed stamp is ignored, never rendered
    assert (
        _format_messages([{"role": "user", "content": "x", "timestamp": "yesterday"}]) == "[user] x"
    )


def test_cited_facts_take_the_turn_time_when_only_the_day_is_known() -> None:
    msgs = [
        {"role": "user", "content": "a", "timestamp": "2024-10-01T08:00"},
        {"role": "user", "content": "b", "timestamp": "2024-10-01T08:10"},
        {"role": "user", "content": "c"},
    ]
    facts = [
        ExtractedFact(memory_key="f1", value="felt belittled", source_message_indices=[1]),
        ExtractedFact(
            memory_key="f2",
            value="day anchor",
            temporal_anchor="2024-10-01T00:00:00",
            valid_at="2024-10-01T00:00:00",
            source_message_indices=[0, 1],
        ),
        ExtractedFact(
            memory_key="f3",
            value="own time",
            temporal_anchor="2024-10-01T17:30:00",
            source_message_indices=[1],
        ),
        ExtractedFact(
            memory_key="f4",
            value="other date",
            temporal_anchor="2024-09-20T00:00:00",
            source_message_indices=[1],
        ),
        ExtractedFact(memory_key="f5", value="uncited"),
        ExtractedFact(memory_key="f6", value="cites unstamped", source_message_indices=[2]),
    ]
    out = {f.memory_key: f for f in apply_message_timestamps(facts, msgs)}
    assert (
        out["f1"].temporal_anchor == "2024-10-01T08:10:00"
        and out["f1"].valid_at == "2024-10-01T08:10:00"
    )
    assert out["f2"].temporal_anchor == "2024-10-01T08:10:00"  # latest cited turn
    assert out["f3"].temporal_anchor == "2024-10-01T17:30:00"  # model's own time wins
    assert out["f4"].temporal_anchor == "2024-09-20T00:00:00"  # different date wins
    assert out["f5"].temporal_anchor is None and out["f6"].temporal_anchor is None
    # no stamps anywhere → identity
    assert apply_message_timestamps(facts, [{"role": "user", "content": "a"}]) == facts


def test_recorded_date_keeps_a_turn_time_and_drops_midnight() -> None:
    class _M:
        def __init__(self, v: Any) -> None:
            self.valid_at = v
            self.created_at = None

    assert _recorded_date(_M("2024-10-01T08:10:00")) == "2024-10-01 08:10"
    assert _recorded_date(_M("2024-10-01T00:00:00")) == "2024-10-01"
    assert _recorded_date(_M("2024-10-01")) == "2024-10-01"


def test_evidence_rows_observe_the_message_time(monkeypatch) -> None:
    import server.memory.app.extract.evidence as ev

    captured: list[Any] = []

    async def _write(items):
        captured.extend(items)

        class _Row:
            evidence_id = "e"

        return [_Row() for _ in items]

    monkeypatch.setattr("server.memory.app.evidence.write_evidence_rows", _write)
    inp = MemorySetFromMessagesInput(
        org="o", namespace="n", agent="a", messages=[{"role": "user", "content": "x"}]
    )
    asyncio.run(
        ev._write_source_evidence(
            inp,
            [
                {"role": "user", "content": "x", "timestamp": "2024-10-01T08:10"},
                {"role": "assistant", "content": "y"},
            ],
        )
    )
    assert captured[0].observed_at == datetime(2024, 10, 1, 8, 10)
    assert captured[1].observed_at is None  # → write time


def test_chunk_lines_carry_the_turn_clock(monkeypatch) -> None:
    """Verbatim chunks have no valid_at column; a timestamped turn keeps its
    clock inside the text so a chunk-only answer can still say the minute."""
    from server.memory.app import chunk_projection as chunk_projection

    captured: dict[str, Any] = {}

    def _split(text: str):
        captured["text"] = text
        return [], 0

    monkeypatch.setattr(chunk_projection, "chunk_text_with_truncation", _split)
    inp = MemorySetFromMessagesInput(
        org="o", namespace="n", agent="a", messages=[{"role": "user", "content": "x"}]
    )
    asyncio.run(
        chunk_projection.write_memory_chunks(
            inp,
            [
                {
                    "role": "user",
                    "content": "The clock ticked loudly.",
                    "timestamp": "2024-10-01T08:10",
                },
                {"role": "assistant", "content": "Go on."},
            ],
            parent_memory_keys=[],
            evidence_ids=[],
        )
    )
    assert captured["text"].splitlines() == [
        "[user @ 2024-10-01 08:10] The clock ticked loudly.",
        "[assistant] Go on.",
    ]
