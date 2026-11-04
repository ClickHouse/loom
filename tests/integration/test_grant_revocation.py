"""A token's claim reaches no further than the grant its subject holds there,
over real HTTP against a server that requires auth.

Each case first proves the token works, then takes the grant away the way an
operator or the subject would, then replays what used to bring the access
back: a self-join, a fresh join request, a re-request, an approval below the
current role. A claim never backed by a grant keeps working.
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
_KID = "default"  # the root secret signs as `default`
_ISS, _AUD = "it-grant-revocation-issuer", "it-grant-revocation-audience"
ORG = unique_org("grant-revocation")


@pytest.fixture(scope="module")
def base(control_plane_url: str) -> Iterator[str]:
    env = {
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_JWT_SECRET": _SECRET,
        "LOOM_JWT_ISSUER": _ISS,
        "LOOM_JWT_AUDIENCE": _AUD,
        "LOOM_JWT_PUBLIC_KEYS_JSON": "",
        "LOOM_JWT_TRUSTED_ISSUERS": "",
        "LOOM_SELF_SIGNUP": "open",
        "LOOM_SELF_SIGNUP_ORG": ORG,
    }
    with loom_server("grant-revocation", env) as url:
        yield url


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


def test_a_claim_never_backed_by_a_grant_keeps_working_until_a_grant_row_exists(
    base: str,
) -> None:
    # An operator-named claim stands where the subject holds no grant row; a
    # request, even one rejected, is a row, and caps the claim at no access.
    ns = _namespace(base, "approval", alice=None)
    bob = _token("bob", [ns])
    assert _writes(base, bob, ns) == 200
    asked = _call(base, "namespace.join", bob, namespace=ns, role="writer")
    assert asked.json()["status"] == "pending", asked.text
    rejected = _call(
        base, "grants.approve", _admin(), namespace=ns, subject="bob", decision="reject"
    )
    assert rejected.status_code == 200, rejected.text
    assert _writes(base, bob, ns) == 403


def test_leaving_ends_every_tokens_claim(base: str) -> None:
    ns = _namespace(base, "approval")
    old = _token("alice", [ns])
    assert _call(base, "namespace.leave", old, namespace=ns).json()["left"]
    assert _writes(base, old, ns) == 403
    assert _writes(base, _token("alice", [ns]), ns) == 403


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
    token = _login()
    assert _writes(base, token, ns) == 403
    assert _reads(base, token, ns) == 200


def test_a_self_signup_naming_someone_elses_address_holds_none_of_its_grants(base: str) -> None:
    # Bob's address is granted before he has an account; mallory self-signs up
    # with it. Self-signup never verifies the email, so the grant is not hers: a
    # token claiming the namespace is capped at no access, and her listing omits it.
    ns = _namespace(base, "approval", alice=None)
    bob = f"bob-{ns}@example.com"
    granted = _call(base, "grants.create", _admin(), namespace=ns, subject=bob, role="writer")
    assert granted.status_code == 200, granted.text
    mallory, password = f"m-{ns}", secrets.token_urlsafe(16)
    signup = httpx.post(
        f"{base}/v1/auth/self-signup",
        json={"namespace": mallory, "email": bob, "password": password},
        timeout=15.0,
    )
    assert signup.status_code == 200, signup.text
    assert _writes(base, _token(signup.json()["subject"], [ns]), ns) == 403
    login = httpx.post(
        f"{base}/v1/auth/login", json={"identifier": bob, "password": password}, timeout=15.0
    )
    assert login.status_code == 200, login.text
    mine = _call(base, "namespace.list_mine", str(login.json()["token"]))
    assert mine.status_code == 200, mine.text
    assert ns not in mine.text
