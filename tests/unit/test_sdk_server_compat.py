"""An SDK at X.Y against a server at X.(Y-1) (docs/operations/supported-versions.md):
an operation the older server does not serve fails with `NotFound`, never as a
silent success."""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from fastapi import FastAPI
from loom import Agent
from loom.errors import NotFound

from server.operations.registry import ALL_OPERATIONS
from server.transport import http as http_mod


async def _never(inp: object) -> object:
    raise AssertionError("an op the older server does not serve never runs")


@pytest.fixture()
def older_server(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Every op but `memory.set`, which this server predates."""
    ops = [
        dataclasses.replace(op, handler=_never)
        for op in ALL_OPERATIONS
        if op.name != "memory.set"
    ]
    assert len(ops) == len(ALL_OPERATIONS) - 1
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    return app


async def test_an_op_the_older_server_does_not_serve_raises_not_found(
    older_server: FastAPI,
) -> None:
    agent = Agent("acme", "support", "assistant", base_url="http://loom", auto_run=False)
    agent.transport._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=older_server), base_url="http://loom"
    )
    try:
        with pytest.raises(NotFound):
            await agent.call("memory.set", {"memory_key": "k", "value": "v"})
    finally:
        await agent.transport._client.aclose()
