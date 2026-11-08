"""The transport negotiates the protocol version and answers
discovery.

`initialize` echoes a version this transport serves and answers the latest
otherwise; `server/discover` lists the served versions with the server's
capabilities and identity; a request whose `_meta` names an unserved version
gets `UnsupportedProtocolVersionError` (-32022) with the served list; an
unserved `MCP-Protocol-Version` header is HTTP 400 with the same frame; the
list results carry `ttlMs` / `cacheScope`; the listing order is stable.

Hermetic: the primer render is stubbed; the endpoint runs in-process.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.transport.mcp as mcp
from server.transport import mcp_config, mcp_primer
from server.transport.mcp_config import (
    MCP_PROTOCOL_VERSION,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    negotiate_protocol_version,
    protocol_version_supported,
    requested_meta_version,
)

META = "io.modelcontextprotocol/protocolVersion"


@pytest.fixture()
def _client(monkeypatch: pytest.MonkeyPatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.settings import settings

    monkeypatch.setattr(settings, "identity_provider", "single-user")

    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return "PRIMER"

    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    app = FastAPI()
    mcp.attach(app)
    return TestClient(app)


def _rpc(method: str, params: dict[str, Any] | None = None, rid: Any = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


# ---- pure helpers ----


def test_supported_versions_are_latest_first_and_exclude_the_stateless_revision() -> None:
    assert MCP_SUPPORTED_PROTOCOL_VERSIONS[0] == MCP_PROTOCOL_VERSION == "2025-11-25"
    assert list(MCP_SUPPORTED_PROTOCOL_VERSIONS) == sorted(
        MCP_SUPPORTED_PROTOCOL_VERSIONS, reverse=True
    )
    assert "2026-07-28" not in MCP_SUPPORTED_PROTOCOL_VERSIONS
    assert protocol_version_supported("2025-06-18")
    assert not protocol_version_supported("2026-07-28")
    assert not protocol_version_supported(None)
    assert not protocol_version_supported(20251125)


def test_negotiate_echoes_a_served_version_and_answers_the_latest_otherwise() -> None:
    assert negotiate_protocol_version("2025-06-18") == "2025-06-18"
    assert negotiate_protocol_version("2024-11-05") == "2024-11-05"
    assert negotiate_protocol_version("2026-07-28") == "2025-11-25"
    assert negotiate_protocol_version("1.0.0") == "2025-11-25"
    assert negotiate_protocol_version(None) == "2025-11-25"


def test_requested_meta_version_reads_only_the_documented_key() -> None:
    assert (
        requested_meta_version(_rpc("tools/list", {"_meta": {META: "2026-07-28"}})) == "2026-07-28"
    )
    assert requested_meta_version(_rpc("tools/list")) is None
    assert requested_meta_version(_rpc("tools/list", {"_meta": {META: 7}})) is None
    assert requested_meta_version(_rpc("tools/list", {"_meta": "x"})) is None
    assert requested_meta_version(["not", "a", "dict"]) is None


# ---- initialize ----


def test_initialize_echoes_the_clients_version_when_served(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return ""

    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    for v in MCP_SUPPORTED_PROTOCOL_VERSIONS:
        resp = asyncio.run(
            mcp._handle_request(_rpc("initialize", {"protocolVersion": v}), auth=None)
        )
        assert resp["result"]["protocolVersion"] == v, v


def test_initialize_answers_the_latest_served_version_to_an_unknown_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return ""

    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    for v in ("2026-07-28", "1.0.0", None):
        params: dict[str, Any] = {} if v is None else {"protocolVersion": v}
        resp = asyncio.run(mcp._handle_request(_rpc("initialize", params), auth=None))
        assert resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION, v


# ---- server/discover ----


def test_discover_lists_served_versions_capabilities_identity_and_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _instr(auth: Any, toolset: str | None = None) -> str:
        return "SESSION-START-PRIMER"

    monkeypatch.setattr(mcp, "_initialize_instructions", _instr)
    resp = asyncio.run(
        mcp._handle_request(_rpc("server/discover", {"_meta": {META: "2026-07-28"}}), auth=None)
    )
    r = resp["result"]
    assert r["resultType"] == "complete"
    assert r["supportedVersions"] == list(MCP_SUPPORTED_PROTOCOL_VERSIONS)
    assert set(r["capabilities"]) == {"tools", "prompts", "resources"}
    assert r["_meta"]["io.modelcontextprotocol/serverInfo"] == mcp.SERVER_INFO
    assert r["instructions"] == "SESSION-START-PRIMER"
    assert r["cacheScope"] == "private" and r["ttlMs"] > 0


def test_discover_is_answered_whatever_version_the_probe_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe exists to be answered before the client has picked a version."""

    async def _boom(auth: Any, toolset: str | None = None) -> str:
        raise RuntimeError("CH down")

    monkeypatch.setattr(mcp, "_initialize_instructions", _boom)
    resp = asyncio.run(
        mcp._handle_request(_rpc("server/discover", {"_meta": {META: "2099-01-01"}}), auth=None)
    )
    assert "error" not in resp
    assert "instructions" not in resp["result"]  # render failure is non-fatal
    assert resp["result"]["supportedVersions"][0] == MCP_PROTOCOL_VERSION


# ---- _meta version on ordinary requests ----


def test_unserved_meta_version_gets_the_recognised_modern_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = asyncio.run(
        mcp._handle_request(
            _rpc("tools/list", {"_meta": {META: "2026-07-28"}}, rid="d-1"), auth=None
        )
    )
    err = resp["error"]
    assert resp["id"] == "d-1"
    assert err["code"] == UNSUPPORTED_PROTOCOL_VERSION == -32022
    assert err["data"] == {
        "supported": list(MCP_SUPPORTED_PROTOCOL_VERSIONS),
        "requested": "2026-07-28",
    }


def test_served_meta_version_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = asyncio.run(
        mcp._handle_request(_rpc("ping", {"_meta": {META: "2025-11-25"}}), auth=None)
    )
    assert resp == {"jsonrpc": "2.0", "id": 1, "result": {}}


# ---- MCP-Protocol-Version header ----


def test_unserved_header_version_is_http_400_with_the_modern_error(_client) -> None:
    resp = _client.post(
        "/mcp", json=_rpc("ping", rid=9), headers={"MCP-Protocol-Version": "2026-07-28"}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["id"] == 9
    assert body["error"]["code"] == -32022
    assert body["error"]["data"]["supported"][0] == MCP_PROTOCOL_VERSION


def test_served_or_absent_header_version_is_served(_client) -> None:
    ok = _client.post("/mcp", json=_rpc("ping"), headers={"MCP-Protocol-Version": "2025-06-18"})
    assert ok.status_code == 200 and ok.json()["result"] == {}
    absent = _client.post("/mcp", json=_rpc("ping"))
    assert absent.status_code == 200 and absent.json()["result"] == {}


def test_header_check_precedes_auth_and_keeps_the_frame_id_a_string_or_number(
    _client, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = _client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"MCP-Protocol-Version": "1900-01-01"},
    )
    assert resp.status_code == 400
    rid = resp.json()["id"]
    assert isinstance(rid, (str, int)) and rid is not None


# ---- cache hints + deterministic listing ----


def test_list_results_carry_cache_hints_and_the_primer_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = asyncio.run(mcp._handle_request(_rpc("tools/list"), auth=None))["result"]
    prompts = asyncio.run(mcp._handle_request(_rpc("prompts/list"), auth=None))["result"]
    resources = asyncio.run(mcp._handle_request(_rpc("resources/list"), auth=None))["result"]
    for r in (tools, prompts, resources):
        assert r["cacheScope"] == "public" and r["ttlMs"] == 300_000

    async def _primer(auth: Any, *, toolset: str | None = None) -> str:
        return "PRIMER"

    monkeypatch.setattr(mcp_primer, "_loom_primer_text", _primer)
    read = asyncio.run(
        mcp._handle_request(
            _rpc("resources/read", {"uri": mcp_config._LOOM_PRIMER_RESOURCE_URI}), auth=None
        )
    )["result"]
    assert read["cacheScope"] == "private" and read["ttlMs"] == 60_000
    assert read["contents"][0]["text"] == "PRIMER"


def test_tools_list_order_is_deterministic() -> None:
    a = asyncio.run(mcp._handle_request(_rpc("tools/list"), auth=None))["result"]["tools"]
    b = asyncio.run(mcp._handle_request(_rpc("tools/list"), auth=None))["result"]["tools"]
    assert [t["name"] for t in a] == [t["name"] for t in b]
    assert len(a) == len({t["name"] for t in a})
