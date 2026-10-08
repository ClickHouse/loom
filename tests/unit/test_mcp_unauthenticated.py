"""An MCP client that fails authentication is told so in HTTP.

Under any non-open `mcp_auth_mode`, a missing, malformed or expired bearer on
POST /mcp (and DELETE /mcp) answers HTTP 401 with an RFC 6750
`WWW-Authenticate: Bearer` challenge — regardless of `oauth_enabled`. The
JSON-RPC error frame in the body echoes the request's id and is never
`id: null` (MCP's `JSONRPCError.id` is string | number; the reference
TypeScript SDK rejects null with a zod union error). Notifications answer
202 with an empty body. Auth-off / `open` mode is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.auth.jwt import mint_token  # noqa: E402
from server.settings import settings  # noqa: E402

INVALID_REQUEST = -32600


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "required",
    oauth: bool = False,
    auth_required: bool = True,
) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", auth_required)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", mode)
    monkeypatch.setattr(settings, "oauth_enabled", oauth)
    from server.transport import mcp as mcp_mod

    app = FastAPI()
    mcp_mod.attach(app)
    return TestClient(app)


def _mint(ttl_seconds: int = 300) -> str:
    return mint_token(
        subject="mcp-8500@dev",
        org="dev",
        namespaces=["*"],
        roles=["writer"],
        ttl_seconds=ttl_seconds,
        is_service_account=True,
    )


def _initialize(req_id: object = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "initialize", "params": {}}


def _assert_challenge(r, *, invalid_token: bool) -> str:
    assert r.status_code == 401, r.text
    wa = r.headers.get("WWW-Authenticate", "")
    assert wa.startswith('Bearer realm="loom"'), wa
    if invalid_token:
        assert 'error="invalid_token"' in wa and 'error_description="' in wa, wa
    else:
        # RFC 6750 §3.1: a request with no token gets no error code.
        assert "error=" not in wa, wa
    return wa


def _assert_frame_id_echoes(r, expected: object) -> None:
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == INVALID_REQUEST
    assert body["id"] == expected
    assert isinstance(body["id"], (str, int)) and not isinstance(body["id"], bool)


# --- garbage / expired / missing bearer on POST -----------------------------


def test_garbage_bearer_is_401_with_invalid_token_challenge(monkeypatch) -> None:
    """The live repro: `Authorization: Bearer garbage`, OAuth off → 401, not
    200 + `id: null`."""
    c = _client(monkeypatch)
    r = c.post("/mcp", json=_initialize(1), headers={"Authorization": "Bearer garbage"})
    _assert_challenge(r, invalid_token=True)
    _assert_frame_id_echoes(r, 1)
    assert "unauthenticated" in r.json()["error"]["message"]


def test_expired_bearer_is_401_with_invalid_token_challenge(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post("/mcp", json=_initialize("req-7"), headers={"Authorization": f"Bearer {_mint(-60)}"})
    wa = _assert_challenge(r, invalid_token=True)
    assert "expired" in wa
    _assert_frame_id_echoes(r, "req-7")


def test_missing_bearer_is_401_without_error_param(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post("/mcp", json=_initialize("abc"))
    wa = _assert_challenge(r, invalid_token=False)
    assert wa == 'Bearer realm="loom"'
    _assert_frame_id_echoes(r, "abc")
    assert "missing bearer token (initialize requires auth" in r.json()["error"]["message"]


def test_malformed_authorization_header_is_401(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post("/mcp", json=_initialize(3), headers={"Authorization": "Basic abc"})
    _assert_challenge(r, invalid_token=True)
    _assert_frame_id_echoes(r, 3)


def test_error_frame_id_is_never_null(monkeypatch) -> None:
    """No id in the request (or no JSON-RPC shape at all) → the frame still
    carries a string id, because null fails the SDK's schema."""
    c = _client(monkeypatch)
    bad = {"Authorization": "Bearer garbage"}
    for body in ({"jsonrpc": "2.0", "method": "initialize"}, {"not": "jsonrpc"}, [1, 2]):
        r = c.post("/mcp", json=body, headers=bad)
        assert r.status_code == 401, r.text
        rid = r.json()["id"]
        assert isinstance(rid, str) and rid, r.text


def test_batch_missing_bearer_echoes_first_request_id(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post(
        "/mcp",
        json=[{"jsonrpc": "2.0", "method": "notifications/initialized"}, _initialize(9)],
    )
    _assert_challenge(r, invalid_token=False)
    _assert_frame_id_echoes(r, 9)


def test_discovery_only_open_method_without_bearer_is_200(monkeypatch) -> None:
    """A missing bearer is only an auth failure on a gated method."""
    c = _client(monkeypatch, mode="discovery_only")
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {}
    assert "WWW-Authenticate" not in r.headers


def test_discovery_only_garbage_bearer_is_401(monkeypatch) -> None:
    """A token that is present but bad is rejected even on an open method."""
    c = _client(monkeypatch, mode="discovery_only")
    r = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "ping"},
        headers={"Authorization": "Bearer garbage"},
    )
    _assert_challenge(r, invalid_token=True)


def test_required_gates_discover_prompts_and_resources_like_initialize(monkeypatch) -> None:
    """`server/discover` answers the same instructions
    `initialize` does, and the prompt / primer resource render for the
    caller; in `required` mode none of them is open without a bearer. Only
    notifications pass."""
    c = _client(monkeypatch, mode="required")
    for i, method in enumerate(
        ["server/discover", "prompts/list", "prompts/get", "resources/list", "resources/read"]
    ):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 100 + i, "method": method, "params": {}})
        assert r.status_code == 401, (method, r.text)
        _assert_challenge(r, invalid_token=False)
        _assert_frame_id_echoes(r, 100 + i)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202, r.text


def test_discovery_only_leaves_discover_open(monkeypatch) -> None:
    """The probe a client sends before choosing a protocol version is a
    discovery method: open where `initialize` is open."""
    c = _client(monkeypatch, mode="discovery_only")
    r = c.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "server/discover",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["supportedVersions"][0] == "2025-11-25"


# --- independence from OAuth; open mode unchanged ---------------------------


def test_oauth_on_keeps_resource_metadata_and_adds_realm(monkeypatch) -> None:
    c = _client(monkeypatch, oauth=True)
    r = c.post("/mcp", json=_initialize(1))
    wa = _assert_challenge(r, invalid_token=False)
    assert 'resource_metadata="' in wa and "/.well-known/oauth-protected-resource" in wa
    r = c.post("/mcp", json=_initialize(2), headers={"Authorization": "Bearer garbage"})
    wa = _assert_challenge(r, invalid_token=True)
    assert 'resource_metadata="' in wa
    _assert_frame_id_echoes(r, 2)


def test_open_mode_garbage_bearer_still_falls_back_to_no_auth(monkeypatch) -> None:
    c = _client(monkeypatch, mode="open")
    r = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "ping"},
        headers={"Authorization": "Bearer garbage"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {}
    assert "WWW-Authenticate" not in r.headers


def test_auth_off_deployment_is_unchanged(monkeypatch) -> None:
    c = _client(monkeypatch, auth_required=False)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "ping"})
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {}


def test_valid_bearer_is_200(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 8, "method": "ping"},
        headers={"Authorization": f"Bearer {_mint()}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"jsonrpc": "2.0", "id": 8, "result": {}}


def test_token_in_the_url_is_not_a_credential(monkeypatch) -> None:
    """A valid JWT in `?token=` authenticates nothing: request lines are logged,
    so the header is the only place the bearer is read."""
    c = _client(monkeypatch)
    r = c.post(f"/mcp?token={_mint()}", json=_initialize("url-1"))
    wa = _assert_challenge(r, invalid_token=False)
    assert wa == 'Bearer realm="loom"'
    _assert_frame_id_echoes(r, "url-1")


def test_header_bearer_still_wins_beside_other_query_params(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post(
        "/mcp?toolset=coding&token=garbage",
        json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
        headers={"Authorization": f"Bearer {_mint()}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"jsonrpc": "2.0", "id": 9, "result": {}}


# --- notifications → 202 empty ----------------------------------------------


def test_notification_is_202_with_empty_body(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {_mint()}", "Mcp-Session-Id": "s-8500"},
    )
    assert r.status_code == 202, r.text
    assert r.content == b""
    assert r.headers.get("Mcp-Session-Id") == "s-8500"


def test_all_notification_batch_is_202_with_empty_body(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
        ],
        headers={"Authorization": f"Bearer {_mint()}"},
    )
    assert r.status_code == 202, r.text
    assert r.content == b""


def test_mixed_batch_stays_200_list(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
        ],
        headers={"Authorization": f"Bearer {_mint()}"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == [{"jsonrpc": "2.0", "id": 10, "result": {}}]


# --- DELETE /mcp ------------------------------------------------------------


def test_delete_garbage_bearer_is_401(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.delete("/mcp", headers={"Authorization": "Bearer garbage", "Mcp-Session-Id": "x"})
    _assert_challenge(r, invalid_token=True)
    assert isinstance(r.json()["id"], str)


def test_delete_missing_bearer_is_401(monkeypatch) -> None:
    c = _client(monkeypatch)
    r = c.delete("/mcp", headers={"Mcp-Session-Id": "x"})
    wa = _assert_challenge(r, invalid_token=False)
    assert wa == 'Bearer realm="loom"'
    assert isinstance(r.json()["id"], str)


def test_an_unloaded_revocation_list_is_a_retryable_503_not_a_401(monkeypatch) -> None:
    """A 401 tells the client its token is bad and it throws a good one away."""
    import server.auth.revocation as rev

    monkeypatch.setattr(rev, "_loaded", False)
    c = _client(monkeypatch)
    r = c.post("/mcp", json=_initialize(3), headers={"Authorization": f"Bearer {_mint()}"})
    assert r.status_code == 503, r.text
    assert r.headers.get("Retry-After") == "5"
    assert "WWW-Authenticate" not in r.headers
    assert r.json()["id"] == 3


@pytest.mark.parametrize("typo", ["Open", "discovery", "none", ""])
def test_an_unknown_auth_mode_fails_at_load(typo: str) -> None:
    """The mode is a closed set: a typo is refused when the settings load
    instead of booting under whatever branch the transport falls through to."""
    from server.settings import Settings

    with pytest.raises(ValueError, match="mcp_auth_mode"):
        Settings(_env_file=None, mcp_auth_mode=typo)  # type: ignore[call-arg, arg-type]
