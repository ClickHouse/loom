"""The typed kwargs the docs send writers to reach the wire, each with the
value the caller gave (or none when unset)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from loom import Agent


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, op_path: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append((op_path, payload))
        if op_path == "/v1/memory.bulk_set":
            return {
                "written": [
                    {"memory_key": e["memory_key"], "version": 1} for e in payload["entries"]
                ]
            }
        if op_path == "/v1/profile.render":
            return {"rendered": ""}
        return {"version": 1}

    async def close(self) -> None:
        pass

    def last(self, op: str) -> dict[str, Any]:
        return [p for o, p in self.calls if o == op][-1]


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x", auto_run=False)
    a.transport = _FakeTransport()  # type: ignore[assignment]
    return a


async def test_set_declares_source_class(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.memory.set("k", "v", source_class="external")
    assert t.last("/v1/memory.set")["source_class"] == "external"
    await agent.memory.set("k", "v")
    assert "source_class" not in t.last("/v1/memory.set")


async def test_a_coalesced_set_keeps_its_source_class(agent: Agent) -> None:
    t: Any = agent.transport
    await asyncio.gather(*[agent.memory.set(k, "v", source_class="imported") for k in "abcd"])
    entries = t.last("/v1/memory.bulk_set")["entries"]
    assert entries and all(e["source_class"] == "imported" for e in entries)


async def test_pin_sends_what_set_sends(agent: Agent) -> None:
    t: Any = agent.transport
    kw: dict[str, Any] = dict(
        tags=["ops"],
        entities=["E1"],
        if_match_sha256="abc",
        valid_at=datetime(2026, 1, 2, tzinfo=UTC),
        source_resource_id="doc-1",
        source_class="derived",
        agent_id="other",
    )
    await agent.memory.pin("k", "v", **kw)
    await agent.memory.set("k", "v", **kw)  # a lone set takes the point path
    assert t.last("/v1/memory.pin") == t.last("/v1/memory.set")
    assert t.last("/v1/memory.pin")["memory_type"] == "semantic"


async def test_blacklist_add_sweeps(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.blacklist.add("secret", sweep=True)
    assert t.last("/v1/blacklist.add")["sweep"] is True
    await agent.blacklist.add("secret")
    assert "sweep" not in t.last("/v1/blacklist.add")


async def test_share_passes_the_approver(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.memory.share("k", approver="lead@x")
    assert t.last("/v1/memory.promote")["approver"] == "lead@x"


async def test_profile_render_reads_own_user(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.profile.render(user_id="u1", read_scope="own_user")
    assert t.last("/v1/profile.render")["read_scope"] == "own_user"
    await agent.profile.system_prompt(user_id="u1", read_scope="own_session")
    assert t.last("/v1/profile.render")["read_scope"] == "own_session"
    await agent.profile.render()
    assert "read_scope" not in t.last("/v1/profile.render")


_FACT = ("alice", "likes", "tennis")


async def test_every_point_write_carries_its_relations(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.memory.set("k", "v", relations=[_FACT])
    assert t.last("/v1/memory.set")["relations"] == [list(_FACT)]
    await agent.memory.pin("k", "v", relations=[_FACT])
    assert t.last("/v1/memory.pin")["relations"] == [list(_FACT)]
    await agent.memory.update_by_id("m1", "v", relations=[_FACT])
    assert t.last("/v1/memory.update_by_id")["relations"] == [list(_FACT)]
    await agent.memory.update("k", "v", relations=[_FACT])
    assert t.last("/v1/memory.update")["relations"] == [list(_FACT)]
    await agent.memory.set("k", "v")
    assert "relations" not in t.last("/v1/memory.set")


async def test_a_set_with_relations_is_never_coalesced(agent: Agent) -> None:
    t: Any = agent.transport
    await asyncio.gather(*[agent.memory.set(k, "v", relations=[_FACT]) for k in "abcd"])
    assert not [o for o, _ in t.calls if o == "/v1/memory.bulk_set"]
    assert len([o for o, _ in t.calls if o == "/v1/memory.set"]) == 4


async def test_set_from_messages_sends_defer_only_when_given(agent: Agent) -> None:
    """Unset leaves the operator's extraction_defer_agents in charge; an explicit
    False must reach the server, which honours a sent field over the allowlist."""
    t: Any = agent.transport
    turns = [{"role": "user", "content": "I prefer aisle seats"}]
    await agent.memory.set_from_messages(turns)
    assert "defer" not in t.last("/v1/memory.set_from_messages")
    await agent.memory.set_from_messages(turns, defer=False)
    assert t.last("/v1/memory.set_from_messages")["defer"] is False
    await agent.memory.set_from_messages(turns, defer=True)
    assert t.last("/v1/memory.set_from_messages")["defer"] is True
