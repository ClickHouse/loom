"""A malformed JSON-RPC request to /mcp is a JSON-RPC error, never a 500.

A batch entry (or a whole body) that is not an object is an Invalid
Request for that entry, and a `params` / `arguments` that is not an object
is Invalid Params — with and without a bearer (the authenticated
pre-dispatch screen reads `arguments`), so one bad entry never answers
HTTP 500 for the whole batch.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.settings import settings

INVALID_REQUEST = -32600
INVALID_PARAMS = -32602


def _client(monkeypatch: pytest.MonkeyPatch, *, auth: bool) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", auth)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required" if auth else "open")
    monkeypatch.setattr(settings, "oauth_enabled", False)
    from server.transport import mcp as mcp_mod

    app = FastAPI()
    mcp_mod.attach(app)
    return TestClient(app, raise_server_exceptions=False)


def _headers(auth: bool) -> dict[str, str]:
    if not auth:
        return {}
    token = mint_token(
        subject="mcp-malformed@dev",
        org="dev",
        namespaces=["*"],
        roles=["writer"],
        ttl_seconds=300,
        is_service_account=True,
    )
    return {"Authorization": f"Bearer {token}"}


def _call(params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params}


@pytest.mark.parametrize("body", [5, "text", [1], [None]])
def test_non_object_entry_is_an_invalid_request(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    r = _client(monkeypatch, auth=False).post("/mcp", json=body)
    assert r.status_code == 200
    frames = r.json() if isinstance(body, list) else [r.json()]
    assert [f["error"]["code"] for f in frames] == [INVALID_REQUEST]
    assert all(f["id"] is not None for f in frames)


def test_one_bad_batch_entry_keeps_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    body = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}, 3]
    r = _client(monkeypatch, auth=False).post("/mcp", json=body)
    assert r.status_code == 200
    ping, bad = r.json()
    assert ping == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert bad["error"]["code"] == INVALID_REQUEST


@pytest.mark.parametrize("auth", [False, True])
@pytest.mark.parametrize(
    "params",
    [
        [1],
        {"name": "loom_memory_get", "arguments": "x"},
        {"name": "loom_op", "arguments": "x"},
        {"name": "loom_op", "arguments": {"op": "memory.get", "arguments": 4}},
    ],
)
def test_non_object_params_or_arguments_are_invalid_params(
    monkeypatch: pytest.MonkeyPatch, auth: bool, params: Any
) -> None:
    client = _client(monkeypatch, auth=auth)
    r = client.post("/mcp", json=_call(params), headers=_headers(auth))
    assert r.status_code == 200, r.text
    frame = r.json()
    if "error" in frame:
        assert frame["id"] == 9 and frame["error"]["code"] == INVALID_PARAMS
    else:  # loom_op drops a non-object inner `arguments`; the op then validates
        assert frame["result"]["isError"] is True


def test_non_object_params_on_other_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, auth=False)
    for method in ("initialize", "prompts/get", "resources/read"):
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": method, "params": 7})
        assert r.status_code == 200
        assert r.json()["error"]["code"] == INVALID_PARAMS, method


@pytest.mark.parametrize("auth", [False, True])
def test_a_batch_past_its_cap_or_empty_is_refused_before_any_entry_runs(
    monkeypatch: pytest.MonkeyPatch, auth: bool
) -> None:
    from server.transport import mcp as mcp_mod

    client = _client(monkeypatch, auth=auth)
    ran: list[Any] = []
    handle = mcp_mod._handle_request

    async def counted(*args: Any, **kwargs: Any) -> Any:
        ran.append(1)
        return await handle(*args, **kwargs)

    monkeypatch.setattr(mcp_mod, "_handle_request", counted)
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for body in ([ping] * (mcp_mod.MAX_BATCH_ENTRIES + 1), []):
        r = client.post("/mcp", json=body, headers=_headers(auth))
        assert r.status_code == 200 and r.json()["error"]["code"] == INVALID_REQUEST
    assert ran == []
    r = client.post("/mcp", json=[ping] * mcp_mod.MAX_BATCH_ENTRIES, headers=_headers(auth))
    assert r.status_code == 200 and len(r.json()) == mcp_mod.MAX_BATCH_ENTRIES


def test_an_oversized_batch_is_refused_before_the_bearer_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import mcp as mcp_mod

    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}  # needs a bearer
    r = _client(monkeypatch, auth=True).post(
        "/mcp", json=[listing] * (mcp_mod.MAX_BATCH_ENTRIES + 1)
    )
    assert r.status_code == 200 and r.json()["error"]["code"] == INVALID_REQUEST


def test_an_unsupported_protocol_version_answers_400_before_the_batch_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import mcp as mcp_mod

    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    r = _client(monkeypatch, auth=False).post(
        "/mcp",
        json=[ping] * (mcp_mod.MAX_BATCH_ENTRIES + 1),
        headers={"MCP-Protocol-Version": "1999-01-01"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
