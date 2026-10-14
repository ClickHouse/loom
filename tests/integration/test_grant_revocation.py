"""Taking a grant away ends the claims of the subject's older tokens, over real
HTTP against a server that requires auth.

Each case first proves the old token works, then takes the grant away the way
an operator or the subject would, then replays what used to bring the access
back: a self-join, a fresh join request, a re-request, an approval below the
current role. A claim never backed by a grant and a token minted after the
cutoff keep working.
"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from tests.integration._resources import unique_org
from tests.integration._spawn import loom_server

_SECRET = secrets.token_urlsafe(48)
_KID = "it-grant-revocation"
_ISS, _AUD = "it-grant-revocation-issuer", "it-grant-revocation-audience"
ORG = unique_org("grant-revocation")


@pytest.fixture(scope="module")
def base(control_plane_url: str) -> Iterator[str]:
    env = {
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_JWT_SECRET": _SECRET,
        "LOOM_JWT_SECRETS_JSON": "",
        "LOOM_JWT_ACTIVE_KID": _KID,
        "LOOM_JWT_ISSUER": _ISS,
        "LOOM_JWT_AUDIENCE": _AUD,
        "LOOM_JWT_PUBLIC_KEYS_JSON": "",
        "LOOM_JWT_TRUSTED_ISSUERS": "",
        "LOOM_SELF_SIGNUP": "open",
        "LOOM_SELF_SIGNUP_ORG": ORG,
    }
    with loom_server("grant-revocation", env) as url:
        yield url


def _past_the_cutoff_second() -> None:
    """A cutoff covers the whole second of the change, so a token minted in it
    is cut too: wait into the next second before minting one that should not be."""
    time.sleep(1.1)


def _token(sub: str, namespaces: list[str], **claims: Any) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "org": ORG,
        "namespaces": namespaces,
        "roles": ["writer"],
        "iss": _ISS,
        "aud": _AUD,
        "iat": now,
        "exp": now + 600,
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return pyjwt.encode(payload, _SECRET, algorithm="HS256", headers={"kid": _KID})


def _admin() -> str:
    return _token("boss", ["*"], roles=["org_admin"])


def _call(base: str, op: str, token: str, **body: Any) -> httpx.Response:
    return httpx.post(
        f"{base}/v1/{op}",
        json={"org": ORG, **body},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )


def _writes(base: str, token: str, ns: str) -> int:
    return _call(
        base, "memory.set", token, namespace=ns, agent="a", memory_key="t/fact/1", value="x"
    ).status_code


def _reads(base: str, token: str, ns: str) -> int:
    return _call(
        base, "memory.get", token, namespace=ns, agent="a", memory_key="t/fact/1"
    ).status_code


def _namespace(base: str, join_policy: str, *, alice: str | None = "writer") -> str:
    ns = f"gr-{uuid.uuid4().hex[:8]}"
    register = _call(
        base,
        "namespace.register",
        _admin(),
        namespace=ns,
        owner_email="boss",
        visibility="public",
        join_policy=join_policy,
    )
    assert register.status_code == 200, register.text
    if alice:
        grant = _call(base, "grants.create", _admin(), namespace=ns, subject="alice", role=alice)
        assert grant.status_code == 200, grant.text
    _past_the_cutoff_second()
    return ns


def _revoke(base: str, ns: str, subject: str = "alice") -> None:
    out = _call(base, "grants.revoke", _admin(), namespace=ns, subject=subject)
    assert out.status_code == 200 and out.json()["revoked"], out.text


def test_a_viewer_self_join_does_not_give_the_old_token_its_write_back(base: str) -> None:
    ns = _namespace(base, "open")
    old = _token("alice", [ns])
    assert _writes(base, old, ns) == 200
    _revoke(base, ns)
    assert _writes(base, old, ns) == 403
    joined = _call(base, "namespace.join", old, namespace=ns, role="viewer")
    assert joined.json()["status"] == "active", joined.text
    assert _reads(base, old, ns) == 200
    assert _writes(base, old, ns) == 403


def test_a_join_request_does_not_restore_the_old_tokens(base: str) -> None:
    ns = _namespace(base, "approval")
    flat = _token("alice", [ns])
    scoped = _token("alice", [ns], roles=[], ns_roles={ns: "writer"})
    assert _writes(base, flat, ns) == _writes(base, scoped, ns) == 200
    _revoke(base, ns)
    requested = _call(base, "namespace.join", flat, namespace=ns, role="writer")
    assert requested.json()["status"] == "pending", requested.text
    assert _writes(base, flat, ns) == _writes(base, scoped, ns) == 403


def test_lowering_a_grant_lowers_the_older_tokens(base: str) -> None:
    ns = _namespace(base, "approval")
    old = _token("alice", [ns])
    assert _writes(base, old, ns) == 200
    lowered = _call(
        base, "grants.create", _admin(), namespace=ns, subject="alice", role="viewer", replace=True
    )
    assert lowered.status_code == 200, lowered.text
    assert _writes(base, old, ns) == 403
    assert _reads(base, old, ns) == 200


def test_an_approval_never_lowers_a_grant(base: str) -> None:
    ns = _namespace(base, "approval")
    alice = _token("alice", [ns])
    asked = _call(base, "namespace.join", alice, namespace=ns, role="admin")
    assert asked.json()["requested_role"] == "admin", asked.text
    approve = _call(
        base,
        "grants.approve",
        _admin(),
        namespace=ns,
        subject="alice",
        decision="approve",
        role="viewer",
    )
    assert approve.status_code == 409, approve.text
    assert _writes(base, alice, ns) == 200


def test_a_second_request_is_still_only_a_request(base: str) -> None:
    ns = _namespace(base, "approval", alice=None)
    carol = _token("carol", [], roles=["viewer"])
    for role in ("writer", "admin"):
        out = _call(base, "namespace.join", carol, namespace=ns, role=role)
        assert out.json()["status"] == "pending", out.text
    listed = _call(base, "grants.list", _admin(), namespace=ns).json()
    (row,) = [g for g in listed["rows"] if g["subject"] == "carol"]
    assert row["status"] == "pending"


def test_rejecting_a_request_leaves_an_operator_named_claim(base: str) -> None:
    ns = _namespace(base, "approval", alice=None)
    bob = _token("bob", [ns])
    assert _writes(base, bob, ns) == 200
    asked = _call(base, "namespace.join", bob, namespace=ns, role="writer")
    assert asked.json()["status"] == "pending", asked.text
    rejected = _call(
        base, "grants.approve", _admin(), namespace=ns, subject="bob", decision="reject"
    )
    assert rejected.status_code == 200, rejected.text
    assert _writes(base, bob, ns) == 200


def test_leaving_ends_the_claim_and_a_later_token_is_untouched(base: str) -> None:
    ns = _namespace(base, "approval")
    old = _token("alice", [ns])
    assert _call(base, "namespace.leave", old, namespace=ns).json()["left"]
    assert _writes(base, old, ns) == 403
    _past_the_cutoff_second()
    assert _writes(base, _token("alice", [ns]), ns) == 200


def test_signing_in_again_does_not_bring_back_a_revoked_owner(base: str) -> None:
    ns = f"gr-{uuid.uuid4().hex[:8]}"
    email, password = f"{ns}@example.com", secrets.token_urlsafe(16)
    signup = httpx.post(
        f"{base}/v1/auth/self-signup",
        json={"namespace": ns, "email": email, "password": password},
        timeout=15.0,
    )
    assert signup.status_code == 200, signup.text
    owner = signup.json()["subject"]

    def _login() -> str:
        out = httpx.post(
            f"{base}/v1/auth/login",
            json={"identifier": email, "password": password},
            timeout=15.0,
        )
        assert out.status_code == 200, out.text
        return str(out.json()["token"])

    assert _writes(base, _login(), ns) == 200
    co_admin = _call(base, "grants.create", _admin(), namespace=ns, subject="bob", role="admin")
    assert co_admin.status_code == 200, co_admin.text
    _revoke(base, ns, subject=owner)
    assert _writes(base, _login(), ns) == 403
    given_back = _call(base, "grants.create", _admin(), namespace=ns, subject=owner, role="writer")
    assert given_back.status_code == 200, given_back.text
    assert _writes(base, _login(), ns) == 200
    # Lowered to viewer: the next sign-in reads through the viewer grant, no more.
    lowered = _call(
        base, "grants.create", _admin(), namespace=ns, subject=owner, role="viewer", replace=True
    )
    assert lowered.status_code == 200, lowered.text
    _past_the_cutoff_second()
    token = _login()
    assert _writes(base, token, ns) == 403
    assert _reads(base, token, ns) == 200
