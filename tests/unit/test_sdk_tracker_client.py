"""SDK TrackerClient posts the qa.* wire fields it was given and
nothing else — the tracker namespace is named on every call, never defaulted."""

from __future__ import annotations

from typing import Any

import pytest
from loom.tracker import TrackerClient


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(
        self, op_path: str, payload: dict[str, Any], *a: Any, **k: Any
    ) -> dict[str, Any]:
        self.calls.append((op_path, payload))
        return {"ok": True}


class _Agent:
    def __init__(self) -> None:
        self.transport = _Transport()


@pytest.mark.asyncio
async def test_similar_and_item_wire_fields() -> None:
    ag = _Agent()
    c = TrackerClient(ag)  # type: ignore[arg-type]
    await c.similar(namespace="tracker-clickhouse", issue_number=117352, legs=["lexical"])
    op, body = ag.transport.calls[0]
    assert op == "/v1/tracker.similar"
    assert body == {
        "namespace": "tracker-clickhouse",
        "top_k": 10,
        "issue_number": 117352,
        "legs": ["lexical"],
    }
    await c.similar(
        namespace="tracker-clickhouse",
        text="avgWeighted wrong",
        frames=["DB::A::b()"],
        kinds=["issue"],
        top_k=5,
    )
    body = ag.transport.calls[1][1]
    assert body["text"] == "avgWeighted wrong" and body["frames"] == ["DB::A::b()"]
    assert body["kinds"] == ["issue"] and body["top_k"] == 5 and "issue_number" not in body
    await c.item(namespace="tracker-clickhouse", numbers=[1, 2])
    assert ag.transport.calls[2] == (
        "/v1/tracker.item",
        {"namespace": "tracker-clickhouse", "numbers": [1, 2]},
    )


def test_agent_carries_the_tracker_client() -> None:
    from loom.agent import Agent

    agent = Agent(org="acme", namespace="handbook", agent="bot", api_key="k", base_url="http://x")
    assert isinstance(agent.tracker, TrackerClient)


@pytest.mark.asyncio
async def test_binding_posts_the_addressed_namespace_only() -> None:
    ag = _Agent()
    ag.org, ag.namespace = "clickhouse", "default"  # type: ignore[attr-defined]
    c = TrackerClient(ag)  # type: ignore[arg-type]
    await c.binding(namespace="code-clickhouse")
    assert ag.transport.calls[0] == (
        "/v1/tracker.binding",
        {"org": "clickhouse", "namespace": "code-clickhouse"},
    )
