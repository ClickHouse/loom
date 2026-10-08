"""memory.get_by_id / memory.update_by_id — opaque UUID addressing."""

from __future__ import annotations

import sys
import uuid as _uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_set_returns_memory_id_and_get_by_id_resolves(agent) -> None:
    """memory.set returns a UUID; memory.get_by_id resolves to the full row."""
    key = f"by-id-{_uuid.uuid4().hex[:6]}"
    # Bypass the SDK's get() helper to grab the raw memory_id from the
    # set response.
    set_result = await agent.transport.post(
        "/v1/memory.set",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "user_id": "-",
            "session_id": "-",
            "run_id": "-",
            "memory_key": key,
            "memory_type": "semantic",
            "value": {"opinion": "ClickHouse rocks"},
        },
    )
    mid = set_result["memory_id"]
    assert mid

    got = await agent.memory.get_by_id(mid)
    assert got is not None
    assert got["memory_key"] == key
    assert got["namespace"] == agent.namespace
    assert got["value"] == {"opinion": "ClickHouse rocks"}


@pytest.mark.asyncio
async def test_update_by_id_changes_value_and_bumps_version(agent) -> None:
    """memory.update_by_id writes a new row with the same memory_key +
    a bumped version; subsequent memory.get returns the new content."""
    key = f"upd-{_uuid.uuid4().hex[:6]}"
    set_result = await agent.transport.post(
        "/v1/memory.set",
        {
            "org": agent.org,
            "namespace": agent.namespace,
            "agent": agent.agent,
            "user_id": "-",
            "session_id": "-",
            "run_id": "-",
            "memory_key": key,
            "memory_type": "semantic",
            "value": "version one",
        },
    )
    mid = set_result["memory_id"]

    update = await agent.memory.update_by_id(mid, "version two")
    assert update["updated"] is True
    assert update["new_version"] > 0

    fresh = await agent.memory.get(key)
    assert fresh is not None
    assert fresh["value"] == "version two"


@pytest.mark.asyncio
async def test_get_by_id_unknown_returns_none(agent) -> None:
    """Bogus memory_id resolves to None."""
    result = await agent.memory.get_by_id(str(_uuid.uuid4()))
    assert result is None


@pytest.mark.asyncio
async def test_update_by_id_unknown_returns_updated_false(agent) -> None:
    update = await agent.memory.update_by_id(str(_uuid.uuid4()), "anything")
    assert update["updated"] is False
