"""`search.query` is loom_search as a registry operation.

HTTP / the SDKs reach it at `/v1/search.query`; MCP serves it as the
`loom_search` tool through the registry path, listed by its synthetic
descriptor (the listing does not grow a second `loom_search`). Both run the
one handler and so `search_sections`, the one fan-out.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.transport.mcp_call as mcp_call
from server.auth.fanout import Fanout
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings

PAYLOAD = {
    "memory": {"results": [{"memory_key": "k", "content_excerpt": "espresso"}]},
    "knowledge": {"results": []},
    "profile": {"results": []},
    "conversations": {"results": []},
}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from server.memory.app.loom_search import handler as handler_mod
    from server.transport import mcp_outcome

    seen: list[dict[str, Any]] = []

    async def _sections(**kw: Any) -> dict[str, Any]:
        seen.append(kw)
        return {k: dict(v) for k, v in PAYLOAD.items()}

    async def _fanout(org: str) -> Fanout:
        return Fanout(org=org, namespaces=("n", "team"), cap=32)

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "n", "agent": "a"}

    monkeypatch.setattr(handler_mod, "search_sections", _sections)
    monkeypatch.setattr("server.auth.fanout.resolve_fanout", _fanout)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    for mod in (mcp_call, mcp_outcome):
        monkeypatch.setattr(mod, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(settings, "auth_required", False)
    return seen


def test_the_op_is_registered_as_loom_search() -> None:
    op = OPERATIONS_BY_NAME["search.query"]
    assert op.mcp_tool_name == "loom_search" and op.http_path == "/v1/search.query"
    assert op.read_only and op.required_role.name == "VIEWER"
    assert "mcp_core" in op.reach and "mcp_atomic" in op.reach


def test_http_serves_the_sections(calls: list[dict[str, Any]]) -> None:
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    r = TestClient(app).post(
        "/v1/search.query", json={"org": "o", "namespace": "n", "query": "coffee", "top_k": 3}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["memory"]["results"][0]["memory_key"] == "k"
    assert "tracker" not in body and "withheld" not in body
    assert calls[0]["namespaces"] == ["n", "team"] and calls[0]["top_k"] == 3


def test_http_and_mcp_run_the_same_fan_out(calls: list[dict[str, Any]]) -> None:
    from server.memory.api.models_loom_search import SearchInput
    from server.memory.app.loom_search.handler import handle_search

    asyncio.run(
        handle_search(SearchInput(org="o", namespace="n", query="coffee", tags=["x"], top_k=3))
    )
    params = {"name": "loom_search", "arguments": {"query": "coffee", "tags": ["x"], "top_k": 3}}
    out = asyncio.run(mcp_call._call_tool(1, params, auth=None))["result"]
    assert out["isError"] is False
    assert calls[0] == calls[1]


def test_the_listing_carries_one_loom_search() -> None:
    from server.transport import mcp_tools

    for toolset in ("core", "all", "coding", "atomic"):
        names = [d["name"] for d in asyncio.run(mcp_tools._tools_descriptor(toolset=toolset))]
        assert names.count("loom_search") == 1, toolset


def _consent(monkeypatch: pytest.MonkeyPatch, levels: dict[str, str]) -> list[str]:
    """Namespace consent from `levels`; returns the capture level each fan-out
    ran under."""
    from server.memory.app.loom_search import handler as handler_mod
    from server.observability import genai

    async def _resolve(org: str, namespace: str) -> str:
        return levels[namespace]

    seen: list[str] = []

    async def _sections(**kw: Any) -> dict[str, Any]:
        seen.append(genai.current_capture_level())
        return {k: dict(v) for k, v in PAYLOAD.items()}

    monkeypatch.setattr("server.observability.capture_consent.resolve", _resolve)
    monkeypatch.setattr(handler_mod, "search_sections", _sections)
    return seen


def test_an_all_mine_search_captures_at_the_least_consenting_namespace(
    calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home namespace consents to everything; a fanned-out one to nothing:
    the whole search captures nothing, through the registered op."""
    from server.transport.http import build_router

    seen = _consent(monkeypatch, {"n": "all", "team": "off"})
    app = FastAPI()
    app.include_router(build_router())
    r = TestClient(app).post("/v1/search.query", json={"org": "o", "namespace": "n", "query": "q"})
    assert r.status_code == 200, r.text
    assert seen == ["off"]


def test_a_this_scope_search_keeps_the_home_namespace_consent(
    calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_loom_search import SearchInput
    from server.memory.app.loom_search.handler import handle_search
    from server.observability import genai

    seen = _consent(monkeypatch, {"n": "query", "team": "off"})

    async def _run() -> None:
        genai.set_identity(org="o", namespace="n", capture_level="all")
        await handle_search(SearchInput(org="o", namespace="n", query="q", namespace_scope="this"))

    asyncio.run(_run())
    assert seen == ["query"]


def test_an_unresolvable_fan_out_searches_every_leg_as_this(
    calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consent was narrowed over the addressed namespace alone, so no leg may
    read wider than it."""
    from server.memory.api.models_loom_search import SearchInput
    from server.memory.app.loom_search.handler import handle_search

    async def _no_fanout(org: str) -> Fanout:
        raise RuntimeError("grants unreadable")

    async def _resolve(org: str, namespace: str) -> str:
        return "all"

    monkeypatch.setattr("server.auth.fanout.resolve_fanout", _no_fanout)
    monkeypatch.setattr("server.observability.capture_consent.resolve", _resolve)
    asyncio.run(handle_search(SearchInput(org="o", namespace="n", query="q")))
    assert calls[-1]["namespace_scope"] == "this"
    assert calls[-1]["namespaces"] == ["n"]


def test_unresolvable_consent_or_fan_out_never_widens_capture(
    calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_loom_search import SearchInput
    from server.memory.app.loom_search.handler import handle_search
    from server.observability import genai

    seen = _consent(monkeypatch, {"n": "all"})

    async def _no_fanout(org: str) -> Fanout:
        raise RuntimeError("grants unreadable")

    async def _run(start: str) -> None:
        genai.set_identity(org="o", namespace="n", capture_level=start)
        await handle_search(SearchInput(org="o", namespace="n", query="q"))

    # The fan-out cannot be resolved: the search narrows to the home namespace
    # and never raises the level the transport set.
    monkeypatch.setattr("server.auth.fanout.resolve_fanout", _no_fanout)
    asyncio.run(_run("query"))

    # A fanned-out namespace whose consent lookup fails captures nothing.
    async def _unknown(org: str) -> Fanout:
        return Fanout(org=org, namespaces=("n", "unknown"), cap=32)

    monkeypatch.setattr("server.auth.fanout.resolve_fanout", _unknown)
    asyncio.run(_run("all"))
    assert seen == ["query", "off"]
