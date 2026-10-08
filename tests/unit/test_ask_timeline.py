"""memory.ask computed timeline: dates and deltas rendered for
the synthesizer instead of left to its arithmetic."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit
from server.memory.domain import ask_timeline
from server.memory.domain.ask_timeline import memory_date, question_date, timeline_block


def _hit(key: str, excerpt: str, **kw: Any) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
        **kw,
    )


def test_memory_date_prefers_the_event_anchor_then_valid_then_created() -> None:
    m = SimpleNamespace(
        temporal_anchor="2023-05-20T00:00:00", valid_at="2023-06-01", created_at="2026-01-01"
    )
    assert memory_date(m) == date(2023, 5, 20)
    assert memory_date(SimpleNamespace(temporal_anchor="", valid_at="2023-06-01 10:00:00")) == date(
        2023, 6, 1
    )
    assert (
        memory_date(SimpleNamespace(temporal_anchor="not a date", valid_at=None, created_at=None))
        is None
    )


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _FrozenDatetime:
        return cls(2025, 12, 31, 23, 30, tzinfo=tz)


def test_question_date_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ask_timeline, "datetime", _FrozenDatetime)
    assert question_date("2023-05-23T12:00:00", "2024-01-01") == date(2023, 5, 23)
    assert question_date(None, "2024-01-01") == date(2024, 1, 1)
    # An unparseable anchor falls through to the next source, and "today" is UTC.
    assert question_date("not a date", "2024-01-01") == date(2024, 1, 1)
    assert question_date(None, "") == date(2025, 12, 31)
    assert question_date("garbage", "also garbage") == date(2025, 12, 31)


def test_timeline_block_computes_deltas_gaps_and_lists_undated() -> None:
    ms = [
        _hit(
            "rafting",
            "went rafting",
            temporal_anchor="2023-05-20T00:00:00",
            temporal_confidence=1.0,
        ),
        _hit(
            "museum", "museum visit", temporal_anchor="2023-03-20T00:00:00", temporal_confidence=0.5
        ),
        _hit("colour", "likes blue"),
    ]
    block, rows = timeline_block(ms, now=date(2023, 5, 23))
    assert rows == 2
    lines = block.split("\n")
    assert lines[0] == "Question date: 2023-05-23."
    # Chronological: the museum (March) before the rafting (May).
    assert lines[1].startswith("- 2023-03-20 [museum]: 2 months (64 days) before the question date")
    assert "pinned loosely" in lines[1]
    assert lines[2].startswith(
        "- 2023-05-20 [rafting]: 3 days before the question date; 61 days after the previous row"
    )
    assert lines[3] == "- undated: [colour]"


def test_timeline_block_is_empty_without_dates_and_caps_rows() -> None:
    assert timeline_block([_hit("a", "x")], now=date(2024, 1, 1)) == ("", 0)
    many = [_hit(f"m{i}", "x", temporal_anchor=f"2024-01-{i + 1:02d}T00:00:00") for i in range(30)]
    block, rows = timeline_block(many, now=date(2024, 2, 1), max_rows=5)
    assert rows == 30 and "25 more dated memories not listed" in block


# ---- through the handler -----------------------------------------------------


class _FakeLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text="3 days ago [rafting].")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace(llm=_FakeLLM(), settings=settings)
    e.search_output = MemorySearchOutput(
        results=[
            _hit(
                "rafting",
                "User went whitewater rafting in the Oregon mountains.",
                temporal_anchor="2023-05-20T00:00:00",
            ),
            _hit("colour", "User's favorite colour is blue."),
        ],
        embedding_model="fake-embed",
    )

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    return e


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "How many days ago did I go on a whitewater rafting trip?",
        "observation_date": "2023-05-23",
    }
    base.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**base))


def _synth_call(env) -> tuple[str, str]:
    """The first SYNTHESIS call; a temporal question's deep selector pick can
    be the first LLM call."""
    return next(c for c in env.llm.calls if "worth opening at full detail" not in c[0])


async def test_temporal_raw_off_renders_no_timeline(env, monkeypatch) -> None:

    monkeypatch.setattr("server.memory.app.ask.pool.looks_temporal", lambda *a, **k: False)
    out = await _run()
    assert out.timeline_rows == 0
    assert "Timeline of the memories" not in _synth_call(env)[1]


async def test_temporal_question_renders_the_computed_deltas(env) -> None:
    out = await _run()
    assert out.timeline_rows == 1
    system, user = _synth_call(env)
    assert "Question date: 2023-05-23." in user
    assert "- 2023-05-20 [rafting]: 3 days before the question date" in user
    assert "- undated: [colour]" in user
    assert "computed day/week/month distances" in system


async def test_a_question_that_does_not_read_time_renders_none(env) -> None:
    out = await _run(query="what is my favorite colour")
    assert out.timeline_rows == 0
