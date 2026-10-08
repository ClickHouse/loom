"""Promoting a seeder candidate persists its source thread on the memory row."""

from __future__ import annotations

import pytest


def _candidate(**over):
    base = {
        "memory_key": "vim_preference",
        "proposed_value": "prefers vim",
        "candidate_id": "c-1",
        "source_kind": "conversation_seeder",
        "confidence_avg": 1.0,
        "memory_type": "preference",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_promote_carries_seeded_thread_into_source_trace(monkeypatch) -> None:
    from server.memory.app import learnings
    from server.memory.app import write as write_mod

    captured = {}

    async def fake_set(inp):
        captured["source_trace"] = inp.source_trace

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    await learnings._promote_via_memory_set(
        "dev",
        "ns",
        _candidate(seeded_from_thread_id="thread-123"),
        "alice",
        "ok",
    )
    assert captured["source_trace"] == "conversation:thread-123"


@pytest.mark.asyncio
async def test_promote_without_seed_leaves_source_trace_empty(monkeypatch) -> None:
    from server.memory.app import learnings
    from server.memory.app import write as write_mod

    captured = {}

    async def fake_set(inp):
        captured["source_trace"] = inp.source_trace

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    await learnings._promote_via_memory_set("dev", "ns", _candidate(), "alice", "ok")
    assert captured["source_trace"] == ""
