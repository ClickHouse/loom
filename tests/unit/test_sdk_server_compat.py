"""An SDK at X.Y against a server at X.(Y-1) (docs/operations/supported-versions.md):
an operation the older server does not serve fails with `NotFound`, never as a
silent success."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from loom import Agent
from loom.errors import NotFound

from server.operations.registry import ALL_OPERATIONS
from server.transport import http as http_mod


@pytest.fixture()
def older_server(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The HTTP router with every op but `memory.set`, which this server predates. In
    the production app, with a valid token, no route or middleware answers a POST to
    an unrouted `/v1/<op>` with anything but 404."""
    ops = [op for op in ALL_OPERATIONS if op.name != "memory.set"]
    assert len(ops) == len(ALL_OPERATIONS) - 1
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    return app


async def test_an_op_the_older_server_does_not_serve_raises_not_found(
    older_server: FastAPI,
) -> None:
    agent = Agent(
        "acme", "support", "assistant", base_url="http://loom", api_key="test", auto_run=False
    )
    await agent.transport._client.aclose()
    agent.transport._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=older_server), base_url="http://loom"
    )
    try:
        with pytest.raises(NotFound):
            await agent.call("memory.set", {"memory_key": "k", "value": "v"})
    finally:
        await agent.transport._client.aclose()
