"""LoomCrewMemory against a running Loom: an explicit save and a task output
sent for extraction come back through recall."""

from __future__ import annotations

import os
import uuid

import pytest
from loom import SyncAgent

from tests.integration._eventually import wait_until

# An SDK extra, outside the server's environment: install crewai to run these.
pytest.importorskip("crewai")

from loom.integrations.crewai import LoomCrewMemory


def _recall(memory: LoomCrewMemory, query: str, word: str) -> bool:
    # A write is searchable once its embedding lands.
    return wait_until(
        lambda: any(word in m.record.content.lower() for m in memory.recall(query, limit=5)),
        description=f"{word!r} recalled",
    )


def test_remember_and_extract_then_recall(control_plane_url: str) -> None:
    with SyncAgent(
        org="test-org",
        namespace=f"test-{uuid.uuid4().hex[:8]}",
        agent="test-agent",
        base_url=control_plane_url,
        api_key=os.environ["LOOM_API_KEY"],
    ) as agent:
        # defer=False: extraction inline, since the test server runs no background workers
        memory = LoomCrewMemory(agent=agent, honour_abstain=False, defer=False)
        record = memory.remember("Alice prefers aisle seats on long flights")
        assert record is not None and record.id.startswith("crewai/")
        assert _recall(memory, "Alice aisle seat", "aisle")
        memory.remember_many(
            ["Task: book Alice's trip. Result: Alice flies to Lisbon every month."]
        )
        assert _recall(memory, "Where does Alice fly every month?", "lisbon")
