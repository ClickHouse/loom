"""memory.outcome / memory.outcome_run under a negative outcome emit `harm`:
the caller relied on the row and the work ended badly.
Handlers run against a scripted fake ClickHouse; usage events are captured at
`memory_repo.record_usage_event`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_temporal import MemoryOutcomeInput, MemoryOutcomeRunInput


class _FakeCH:
    def __init__(self) -> None:
        self.one: list[dict[str, Any] | None] = []
        self.all: list[list[dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql, params=None):
        return self.one.pop(0) if self.one else None

    async def query_all(self, sql, params=None):
        return self.all.pop(0) if self.all else []

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows))


@pytest.fixture
def env(monkeypatch):
    import server.core.ch as ch_mod
    from server.memory import store as memory_repo

    fake = _FakeCH()
    events: list[dict[str, Any]] = []

    async def record(**kw):
        events.append(kw)

    async def get_by_id(memory_id, **kw):
        return SimpleNamespace(memory_id=memory_id, namespace="n")

    monkeypatch.setattr(memory_repo, "record_usage_event", record)
    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(ch_mod, "get_client", lambda: fake)
    return fake, events


def test_outcome_run_negative_emits_one_harm_per_resolved_row(env) -> None:
    from server.memory.app.ops import handle_memory_outcome_run

    fake, events = env
    fake.all.append(
        [
            {"memory_id": "id-a", "memory_key": "k1"},
            {"memory_id": "id-b", "memory_key": "k2"},
        ]
    )
    out = asyncio.run(
        handle_memory_outcome_run(
            MemoryOutcomeRunInput(
                org="o",
                namespace="n",
                outcome="negative",
                memory_keys=["k1", "k2", "missing"],
                weight=2,
            )
        )
    )
    assert out.applied == 2 and out.keys_matched == 3
    assert [(e["memory_id"], e["event_type"], e["event_count"]) for e in events] == [
        ("id-a", "harm", 2),
        ("id-b", "harm", 2),
    ]


def test_outcome_run_positive_and_used_are_unchanged(env) -> None:
    from server.memory.app.ops import handle_memory_outcome_run

    fake, events = env
    fake.all.append([{"memory_id": "id-a", "memory_key": "k1"}])
    asyncio.run(
        handle_memory_outcome_run(
            MemoryOutcomeRunInput(org="o", namespace="n", outcome="positive", memory_keys=["k1"])
        )
    )
    fake.all.append([{"memory_id": "id-a", "memory_key": "k1"}])
    asyncio.run(
        handle_memory_outcome_run(
            MemoryOutcomeRunInput(org="o", namespace="n", outcome="used", memory_keys=["k1"])
        )
    )
    assert [e["event_type"] for e in events] == ["evidence", "used"]


def test_outcome_negative_by_id_emits_harm_and_keeps_the_quarantine_gate_off(
    env, monkeypatch
) -> None:
    from server.memory.app.ops import handle_memory_outcome
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_negative_demote_threshold", 0)
    fake, events = env
    fake.one.append({"evidence_count": 1})  # the read-back after the write
    out = asyncio.run(
        handle_memory_outcome(
            MemoryOutcomeInput(
                org="o",
                namespace="n",
                memory_id="id-a",
                outcome="negative",
                weight=1,
            )
        )
    )
    assert out.recorded and not out.quarantined
    assert [(e["memory_id"], e["event_type"]) for e in events] == [("id-a", "harm")]
    # the `rejection` counter is the quarantine gate and stays off at threshold 0
    assert fake.inserted == []
