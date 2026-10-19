"""`memory.list` and `knowledge.list` send `tags_every` only when it is true: a call
that leaves it off sends no `tags_every` key."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from loom import Agent


def _sent(client: str, **kwargs: Any) -> dict[str, Any]:
    agent = Agent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x")
    sent: list[dict[str, Any]] = []

    async def fake_post(op_path, payload, idempotency_key=None, *, timeout_s=None):
        assert op_path == f"/v1/{client}.list"
        sent.append(payload)
        return {"entries": [], "rows": []}

    agent.transport.post = fake_post  # type: ignore[method-assign]
    try:
        asyncio.run(getattr(agent, client).list(**kwargs))
    finally:
        asyncio.run(agent.close())
    return sent[0]


@pytest.mark.parametrize("client", ["memory", "knowledge"])
def test_tags_every_reaches_the_request(client: str) -> None:
    payload = _sent(client, tags=["a", "b"], tags_every=True)
    assert payload["tags"] == ["a", "b"] and payload["tags_every"] is True


@pytest.mark.parametrize("client", ["memory", "knowledge"])
def test_a_call_without_it_does_not_send_it(client: str) -> None:
    assert "tags_every" not in _sent(client, tags=["a"])
