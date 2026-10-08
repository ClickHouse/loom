"""A usage credit is one event per (namespace, memory_key),
and an evaluation read (reinforce=False) credits nothing."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from server.memory.store import usage as mru


class _CH:
    def __init__(self) -> None:
        self.sql = ""

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql = sql
        # One grouped row per key, as the GROUP BY returns.
        return [
            {"memory_id": "m1", "namespace": "n", "memory_key": "user_task_update"},
            {"memory_id": "m2", "namespace": "n", "memory_key": "user_pr_state"},
        ]


def test_record_usage_for_keys_groups_by_key(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    async def _rec(**kw: Any) -> None:
        events.append((kw["memory_id"], kw["event_type"]))

    monkeypatch.setattr(mru, "record_usage_event", _rec)
    ch = _CH()
    n = asyncio.run(
        mru.record_usage_for_keys(
            "o", {"n": ["user_task_update", "user_pr_state"]}, event_type="recall", agent="a", ch=ch
        )
    )
    assert n == 2 and events == [("m1", "recall"), ("m2", "recall")]
    assert (
        "GROUP BY namespace, memory_key" in ch.sql
        and "argMax(toString(memory_id), updated_at)" in ch.sql
    )


def test_ask_input_carries_reinforce_and_weight() -> None:
    from server.memory.api.models_ask import MemoryAskInput

    f = MemoryAskInput.model_fields
    assert f["reinforce"].default is True and f["reinforcement_weight"].default is None


def test_evaluation_read_credits_nothing(monkeypatch) -> None:
    from server.memory.app.ask import handler as ha

    called: list[str] = []

    async def _boom(*a: Any, **k: Any) -> int:
        called.append("credit")
        return 1

    monkeypatch.setattr(mru, "record_usage_for_keys", _boom)
    inp = SimpleNamespace(reinforce=False, org="o", namespace="n", agent="a")
    res = SimpleNamespace(
        abstain=False,
        source="synthesized",
        answer="x [m1]",
        results=[SimpleNamespace(memory_key="m1")],
    )
    ha._schedule_cited_used(inp, res)  # returns before scheduling anything
    ha._record_contradiction(inp, res)
    assert called == []
