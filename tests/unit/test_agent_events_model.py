"""agent_events.model / llm_calls: the accumulator names the
LLM model that carried an op and both emit sites put it on the event row."""

from __future__ import annotations

from datetime import UTC, datetime

from server.observability.cost_accumulator import _Accumulator
from server.observability.events import AgentEvent


def test_no_llm_call_means_empty_model() -> None:
    acc = _Accumulator()
    acc.add_embed("text-embedding-3-small", 500)
    assert acc.dominant_model() == ""
    assert acc.llm_calls == 0


def test_dominant_model_is_the_costliest_not_the_most_called() -> None:
    acc = _Accumulator()
    acc.add_llm("gpt-4o-mini", 1000, 100)  # cheap
    acc.add_llm("gpt-4o-mini", 1000, 100)
    acc.add_llm("claude-opus-4-8", 1000, 100)  # one call, far costlier
    assert acc.llm_calls == 3
    assert acc.dominant_model() == "claude-opus-4-8"
    assert acc.by_model["gpt-4o-mini"][0] == 2
    assert acc.by_model["claude-opus-4-8"][:3] == [1, 1000, 100]


def test_unpriced_models_fall_back_to_calls_then_first_seen() -> None:
    acc = _Accumulator()
    acc.add_llm("fake-a", 10, 1)
    acc.add_llm("fake-b", 10, 1)
    assert acc.dominant_model() == "fake-a"
    acc.add_llm("fake-b", 10, 1)
    assert acc.dominant_model() == "fake-b"


def test_zero_token_calls_are_not_counted() -> None:
    acc = _Accumulator()
    acc.add_llm("gpt-4o-mini", 0, 0)
    assert acc.llm_calls == 0 and acc.dominant_model() == ""


def test_drain_snapshots_and_zeroes() -> None:
    acc = _Accumulator()
    acc.add_llm("gpt-4o-mini", 1000, 100)
    acc.add_embed("text-embedding-3-small", 400)
    snap = acc.drain()
    assert snap.llm_calls == 1 and snap.dominant_model() == "gpt-4o-mini"
    assert snap.embed_tokens == 400 and snap.cost_usd > 0
    assert acc.llm_calls == 0 and acc.by_model == {} and acc.cost_usd == 0.0
    assert acc.embed_tokens == 0 and acc.dominant_model() == ""
    # The snapshot is independent of later activity.
    acc.add_llm("gpt-4o", 10, 10)
    assert snap.dominant_model() == "gpt-4o-mini"


def test_event_row_carries_model_and_calls() -> None:
    e = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        operation="memory.ask",
        request_id="r",
        latency_ms=1,
        success=True,
        model="gpt-4o-mini",
        llm_calls=2,
    )
    row = e.to_row()
    assert row["model"] == "gpt-4o-mini" and row["llm_calls"] == 2
    # Defaults match the migration's '' / 0 = unknown.
    d = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        operation="memory.get",
        request_id="r",
        latency_ms=1,
        success=True,
    ).to_row()
    assert d["model"] == "" and d["llm_calls"] == 0
