"""End-to-end test for memory.set → memory.get against a real Control Plane + CH.

Run with:
    make dev            # start CH + Redis
    make migrate        # apply schemas
    make server &       # in another terminal
    LOOM_BASE_URL=http://localhost:7777 make test-integration
"""

from __future__ import annotations

import pytest

from server.settings.catalog import OP_BUDGETS_P99_MS


@pytest.mark.asyncio
async def test_health(control_plane_url: str) -> None:
    """Sanity: the server is reachable and reports healthy."""
    import httpx

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=2.0) as c:
        r = await c.get("/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "schema_version" in body


@pytest.mark.asyncio
async def test_operations_listed(control_plane_url: str) -> None:
    """The registry exposes the core memory ops."""
    import httpx

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=2.0) as c:
        r = await c.get("/v1/operations")
        assert r.status_code == 200
        ops = {o["name"]: o for o in r.json()}
        assert "memory.get" in ops
        assert "memory.set" in ops
        assert ops["memory.get"]["idempotent"] is True
        assert ops["memory.set"]["idempotent"] is True
        assert ops["memory.get"]["budget_p99_ms"] == OP_BUDGETS_P99_MS["memory.get"]
        assert ops["memory.set"]["budget_p99_ms"] == OP_BUDGETS_P99_MS["memory.set"]


@pytest.mark.asyncio
async def test_memory_get_missing_returns_none(agent) -> None:
    """Unknown key returns None (not an error)."""
    result = await agent.memory.get("never-written")
    assert result is None


@pytest.mark.asyncio
async def test_memory_set_then_get_roundtrip(agent) -> None:
    """The minimum useful end-to-end: write a memory, read it back."""
    result = await agent.memory.set(
        memory_key="current_pr",
        value={"pr_number": 12345, "repo": "clickhouse"},
        memory_type="semantic",
    )
    version = int(result["version"])
    assert version > 0
    assert result["content_sha256"]

    # Read back
    entry = await agent.memory.get("current_pr")
    assert entry is not None
    assert entry["found"] is True
    assert entry["value"] == {"pr_number": 12345, "repo": "clickhouse"}
    assert entry["memory_type"] == "semantic"
    assert entry["confidence"] == 1.0
    assert entry["version"] == version


@pytest.mark.asyncio
async def test_memory_set_overwrites_with_higher_version(agent) -> None:
    """Two writes to the same key — RMT(version) keeps the newer one after merge."""
    r1 = await agent.memory.set("retry_pattern", {"max": 3}, memory_type="procedural")
    r2 = await agent.memory.set("retry_pattern", {"max": 5}, memory_type="procedural")
    v1, v2 = int(r1["version"]), int(r2["version"])
    assert v2 > v1

    # FINAL on read returns the latest version
    entry = await agent.memory.get("retry_pattern")
    assert entry is not None
    assert entry["value"] == {"max": 5}
    assert entry["version"] == v2


@pytest.mark.asyncio
async def test_per_user_isolation(agent) -> None:
    """user_id partitions memory: alice's prefs don't appear under bob."""
    await agent.memory.set(
        "prefers_json",
        True,
        memory_type="preference",
        user_id="alice",
    )
    bob_entry = await agent.memory.get("prefers_json", user_id="bob")
    assert bob_entry is None

    alice_entry = await agent.memory.get("prefers_json", user_id="alice")
    assert alice_entry is not None
    assert alice_entry["value"] is True


@pytest.mark.asyncio
async def test_get_by_key_finds_the_row_another_agent_wrote(agent) -> None:
    """A get names a key, not the agent that wrote it —
    the same user's row is served under any agent; another user's is not."""
    await agent.memory.set("coffee_type_any_agent", "espresso", memory_type="semantic")
    other = await agent.memory.get("coffee_type_any_agent", agent_id="some-other-agent")
    assert other is not None and other["value"] == "espresso"
    assert (
        await agent.memory.get("coffee_type_any_agent", agent_id="some-other-agent", user_id="bob")
        is None
    )
