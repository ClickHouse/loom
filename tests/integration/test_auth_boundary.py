"""The credential boundary of a server that requires auth, over real HTTP and MCP.

The suite-wide server runs with auth off, and the in-process transport tests
(test_auth) exercise the gate without a socket. This module starts a private
server with LOOM_AUTH_REQUIRED=true and its own issuer, audience, kid and
secret, then sends every malformed, expired, mis-issued or mis-signed
credential shape. Each must answer 401 and never echo the credential. A valid
token for the same body answers 200, so each refusal is the gate and not a
malformed call.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from tests.integration._resources import unique_org
from tests.integration._spawn import loom_server

_SECRET = secrets.token_urlsafe(48)
_KID = "it-boundary"
_ISS, _AUD = "it-boundary-issuer", "it-boundary-audience"
ORG = unique_org("auth-boundary")
NS = f"ab-{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="module")
def auth_server(control_plane_url: str) -> Iterator[str]:
    env = {
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_JWT_SECRET": _SECRET,
        "LOOM_JWT_SECRETS_JSON": "",
        "LOOM_JWT_ACTIVE_KID": _KID,
        "LOOM_JWT_ISSUER": _ISS,
        "LOOM_JWT_AUDIENCE": _AUD,
        "LOOM_JWT_PUBLIC_KEYS_JSON": "",
        "LOOM_JWT_TRUSTED_ISSUERS": "",
    }
    with loom_server("auth-boundary", env) as base:
        yield base


def _claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "boundary",
        "org": ORG,
        "namespaces": [NS],
        "roles": ["writer"],
        "iss": _ISS,
        "aud": _AUD,
        "iat": now,
        "exp": now + 600,
        "jti": uuid.uuid4().hex,
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _hs(claims: dict[str, Any], *, key: str = _SECRET, kid: str = _KID) -> str:
    return pyjwt.encode(claims, key, algorithm="HS256", headers={"kid": kid})


def _b64(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _unsigned(claims: dict[str, Any]) -> str:
    return f"{_b64({'alg': 'none', 'typ': 'JWT', 'kid': _KID})}.{_b64(claims)}."


def _alg_swapped(claims: dict[str, Any]) -> str:
    """HS256-signed body under an RS256 header: an RS256 verifier must not
    take the HMAC secret as a public key."""
    signed = _hs(claims)
    _, body, sig = signed.split(".")
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT', 'kid': _KID})}.{body}.{sig}"


_BODY = {"org": ORG, "namespace": NS, "agent": "a", "memory_key": "boundary-probe"}

_BAD_AUTHORIZATION: dict[str, Callable[[], str | None]] = {
    "missing": lambda: None,
    "bearer without a token": lambda: "Bearer",
    "basic scheme": lambda: "Basic " + base64.b64encode(b"user:pass").decode(),
    "not a jwt": lambda: "Bearer not-a-jwt",
    "alg none": lambda: "Bearer " + _unsigned(_claims()),
    "alg swapped to RS256": lambda: "Bearer " + _alg_swapped(_claims()),
    "wrong secret": lambda: "Bearer " + _hs(_claims(), key=secrets.token_urlsafe(48)),
    "unknown kid": lambda: "Bearer " + _hs(_claims(), kid="no-such-kid"),
    "expired": lambda: (
        "Bearer " + _hs(_claims(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600))
    ),
    "wrong issuer": lambda: "Bearer " + _hs(_claims(iss="someone-else")),
    "wrong audience": lambda: "Bearer " + _hs(_claims(aud="another-platform")),
    "no iat": lambda: "Bearer " + _hs(_claims(iat=None)),
    "no sub": lambda: "Bearer " + _hs(_claims(sub=None)),
    "namespaces as a string": lambda: "Bearer " + _hs(_claims(namespaces=NS)),
}


def _headers(authorization: str | None) -> dict[str, str]:
    return {"Authorization": authorization} if authorization is not None else {}


def test_positive_control_a_well_formed_token_is_admitted(auth_server: str) -> None:
    r = httpx.post(
        f"{auth_server}/v1/memory.get",
        json=_BODY,
        headers=_headers("Bearer " + _hs(_claims())),
        timeout=30,
    )
    assert r.status_code == 200, r.text[:300]


@pytest.mark.parametrize("case", sorted(_BAD_AUTHORIZATION))
def test_http_refuses_the_credential_and_does_not_echo_it(auth_server: str, case: str) -> None:
    authorization = _BAD_AUTHORIZATION[case]()
    r = httpx.post(
        f"{auth_server}/v1/memory.get", json=_BODY, headers=_headers(authorization), timeout=30
    )
    assert r.status_code == 401, f"{case}: {r.status_code} {r.text[:300]}"
    token = (authorization or "").removeprefix("Bearer ").removeprefix("Basic ").strip()
    if len(token) > 8:
        assert token not in r.text, f"{case}: the refusal echoes the credential"


@pytest.mark.parametrize("case", ["missing", "not a jwt", "alg none", "expired", "wrong issuer"])
def test_mcp_refuses_the_credential(auth_server: str, case: str) -> None:
    authorization = _BAD_AUTHORIZATION[case]()
    r = httpx.post(
        f"{auth_server}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**_headers(authorization), "Accept": "application/json, text/event-stream"},
        timeout=30,
    )
    assert r.status_code == 401, f"{case}: {r.status_code} {r.text[:300]}"


def test_a_token_in_the_query_string_is_not_a_credential(auth_server: str) -> None:
    """Only the Authorization header carries a bearer; a URL is logged and cached."""
    token = _hs(_claims())
    r = httpx.post(f"{auth_server}/v1/memory.get?token={token}", json=_BODY, timeout=30)
    assert r.status_code == 401, r.text[:300]
    r = httpx.post(
        f"{auth_server}/mcp?token={token}",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
        timeout=30,
    )
    assert r.status_code == 401, r.text[:300]


def test_a_valid_token_for_another_org_reads_nothing_here(auth_server: str) -> None:
    """Right signature, wrong tenant: the org named in the body is not the
    token's, and the refusal does not say whether the key exists."""
    other = "Bearer " + _hs(_claims(org=unique_org("auth-boundary-other"), namespaces=["*"]))
    r = httpx.post(f"{auth_server}/v1/memory.get", json=_BODY, headers=_headers(other), timeout=30)
    assert r.status_code in (403, 404), r.text[:300]
    assert "boundary-probe" not in r.text
