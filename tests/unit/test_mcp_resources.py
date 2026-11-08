"""MCP resources surface — the protocol-native primer delivery.

`loom://primer` is advertised in initialize capabilities, listed by
resources/list, and rendered by resources/read (same text as the `loom`
prompt and `loom_activate` tool). Hermetic: `_loom_primer_text` is patched
so no ClickHouse/profile render is needed.
"""

from __future__ import annotations

import asyncio

import pytest

import server.transport.mcp as mcp
from server.transport import mcp_primer
from server.transport.mcp_config import _LOOM_PRIMER_RESOURCE_URI


@pytest.fixture(autouse=True)
def _fake_primer(monkeypatch):
    async def _primer(auth, **kwargs):
        return "PRIMER-TEXT"

    monkeypatch.setattr(mcp_primer, "_loom_primer_text", _primer)


def _rpc(method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    return asyncio.run(mcp._handle_request(payload, auth=None))


def test_initialize_advertises_resources_capability():
    resp = _rpc("initialize", {})
    caps = resp["result"]["capabilities"]
    assert "resources" in caps
    assert "prompts" in caps and "tools" in caps


def test_resources_list_returns_primer():
    resp = _rpc("resources/list")
    uris = [r["uri"] for r in resp["result"]["resources"]]
    assert uris == [_LOOM_PRIMER_RESOURCE_URI]
    res = resp["result"]["resources"][0]
    assert res["mimeType"] == "text/markdown"
    assert res["name"]


def test_resources_read_renders_primer():
    resp = _rpc("resources/read", {"uri": _LOOM_PRIMER_RESOURCE_URI})
    contents = resp["result"]["contents"]
    assert contents[0]["uri"] == _LOOM_PRIMER_RESOURCE_URI
    assert contents[0]["text"] == "PRIMER-TEXT"
    assert contents[0]["mimeType"] == "text/markdown"


def test_resources_read_unknown_uri_errors():
    resp = _rpc("resources/read", {"uri": "loom://nope"})
    assert "error" in resp
