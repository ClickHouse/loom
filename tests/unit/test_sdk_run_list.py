"""`Agent.run.list` sends run.list's paging inputs (offset, order_by, q) only
when the caller sets them, so a call that names none sends what it sent before
and a server a release behind sees no key it does not know."""

from __future__ import annotations

import asyncio
from typing import Any

from loom import Agent


def _sent(**kwargs: Any) -> dict[str, Any]:
    agent = Agent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x")
    sent: list[dict[str, Any]] = []

    async def fake_post(op_path, payload, idempotency_key=None, *, timeout_s=None):
        await asyncio.sleep(0)
        assert op_path == "/v1/run.list"
        sent.append(payload)
        return {"runs": [], "total": 0}

    agent.transport.post = fake_post  # type: ignore[method-assign]
    try:
        asyncio.run(agent.run.list(**kwargs))
    finally:
        asyncio.run(agent.close())
    return sent[0]


def test_paging_inputs_reach_the_request() -> None:
    payload = _sent(limit=25, offset=50, order_by="duration_desc", q="deploy")
    assert payload["limit"] == 25
    assert payload["offset"] == 50
    assert payload["order_by"] == "duration_desc"
    assert payload["q"] == "deploy"


def test_a_call_without_them_sends_none_of_them() -> None:
    payload = _sent()
    assert "offset" not in payload and "order_by" not in payload and "q" not in payload
