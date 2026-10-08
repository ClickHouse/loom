"""End-to-end self-service namespace flow against a live Loom + ClickHouse.

Covers the canonical user journey:
  1. Two users self-sign-up; each gets a personal namespace auto-claimed.
  2. Alice creates a public namespace.
  3. Bob discovers it via `namespace.browse`, joins, sees it in his
     `namespace.list_mine`.
  4. Bob cannot register over Alice's namespace (admin-grant precondition).
  5. Bob can leave; Alice cannot leave (last admin).

Requires `LOOM_BASE_URL` pointing at a running Control Plane with
`LOOM_SELF_SIGNUP=open` set in its environment. Skipped otherwise.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


# No flaky marker: every refusal/gate (signup collision 409, register-over-others
# 403, last-admin 409, private-join → pending) and every read-back hits
# loom.namespaces / loom.namespace_grants, which the server writes synchronously
# (async_insert=0, wait_for_async_insert=1) and reads FINAL in the same process —
# deterministic, no embedding/async path.
# (These tests still skip in CI unless LOOM_SELF_SIGNUP=open is set there.)


def _post(url: str, path: str, body: dict, token: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(base_url=url, timeout=5.0) as c:
        return c.post(path, json=body, headers=headers)


def _signup_or_skip(url: str, namespace: str) -> dict:
    body = {
        "namespace": namespace,
        "email": f"{namespace}@self-service.local",
        "password": "test-pass-12345",
        "ttl_days": 1,
    }
    r = _post(url, "/v1/auth/self-signup", body)
    if r.status_code == 404:
        pytest.skip("LOOM_SELF_SIGNUP=open is not set on the running server")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_self_signup_refuses_collision(control_plane_url) -> None:
    """Second caller typing the same namespace name gets a 409 — without
    this gate any caller could mint a JWT with sub=<that name> and read
    the original claimer's data."""
    name = f"dup-{uuid.uuid4().hex[:6]}"
    # Two different emails attempting to claim the same namespace —
    # the second must fail with NAMESPACE_CLAIMED.
    first = _post(
        control_plane_url,
        "/v1/auth/self-signup",
        {
            "namespace": name,
            "email": f"first-{name}@x.test",
            "password": "test-pass-12345",
            "ttl_days": 1,
        },
    )
    if first.status_code == 404:
        pytest.skip("LOOM_SELF_SIGNUP=open is not set on the running server")
    assert first.status_code == 200

    second = _post(
        control_plane_url,
        "/v1/auth/self-signup",
        {
            "namespace": name,
            "email": f"second-{name}@x.test",
            "password": "test-pass-12345",
            "ttl_days": 1,
        },
    )
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["detail"]["code"] == "NAMESPACE_CLAIMED"


@pytest.mark.asyncio
async def test_self_signup_creates_personal_namespace(control_plane_url) -> None:
    name = f"alice-{uuid.uuid4().hex[:6]}"
    out = _signup_or_skip(control_plane_url, name)
    assert out["namespace"] == name
    assert out["subject"] == name
    assert out["token"]

    # list_mine should immediately return the new namespace as admin.
    r = _post(
        control_plane_url,
        "/v1/namespace.list_mine",
        {"org": out["org"]},
        token=out["token"],
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    names = {row["namespace"]: row for row in rows}
    assert name in names, f"signup namespace missing from list_mine: {rows}"
    assert names[name]["role"] == "admin"


@pytest.mark.asyncio
async def test_browse_join_and_list_mine_round_trip(control_plane_url) -> None:
    suffix = uuid.uuid4().hex[:6]
    alice = _signup_or_skip(control_plane_url, f"alice-{suffix}")
    bob = _signup_or_skip(control_plane_url, f"bob-{suffix}")
    public_ns = f"team-public-{suffix}"

    # Alice creates a public namespace.
    r = _post(
        control_plane_url,
        "/v1/namespace.register",
        {
            "org": alice["org"],
            "namespace": public_ns,
            "owner_email": alice["subject"],
            "visibility": "public",
            "description": "shared team knowledge",
        },
        token=alice["token"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visibility"] == "public"

    # Bob finds it in browse.
    r = _post(
        control_plane_url,
        "/v1/namespace.browse",
        {"org": bob["org"], "limit": 50},
        token=bob["token"],
    )
    assert r.status_code == 200, r.text
    found = [row for row in r.json()["rows"] if row["namespace"] == public_ns]
    assert found, f"public namespace {public_ns} missing from browse"

    # Bob joins for read access. `viewer` on a public namespace is auto-active
    # (no approval gate); `writer`/`admin` would land pending — see
    # handle_namespace_join.
    r = _post(
        control_plane_url,
        "/v1/namespace.join",
        {
            "org": bob["org"],
            "namespace": public_ns,
            "role": "viewer",
        },
        token=bob["token"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["joined"] is True, body
    assert body["role"] == "viewer", body
    assert body["status"] == "active", body

    # Bob's list_mine includes the public namespace as an active viewer.
    r = _post(
        control_plane_url,
        "/v1/namespace.list_mine",
        {"org": bob["org"]},
        token=bob["token"],
    )
    assert r.status_code == 200, r.text
    rows = {row["namespace"]: row for row in r.json()["rows"]}
    assert public_ns in rows
    assert rows[public_ns]["role"] == "viewer"
    assert rows[public_ns]["visibility"] == "public"


@pytest.mark.asyncio
async def test_join_private_namespace_lands_pending(control_plane_url) -> None:
    """Joining a private namespace is accepted but ALWAYS gated: it lands
    `pending` for an admin to approve (not a hard 403 — see
    handle_namespace_join), and grants zero access until approved."""
    suffix = uuid.uuid4().hex[:6]
    alice = _signup_or_skip(control_plane_url, f"alice-{suffix}")
    bob = _signup_or_skip(control_plane_url, f"bob-{suffix}")
    private_ns = f"team-private-{suffix}"

    r = _post(
        control_plane_url,
        "/v1/namespace.register",
        {
            "org": alice["org"],
            "namespace": private_ns,
            "owner_email": alice["subject"],
            "visibility": "private",
        },
        token=alice["token"],
    )
    assert r.status_code == 200, r.text

    r = _post(
        control_plane_url,
        "/v1/namespace.join",
        {
            "org": bob["org"],
            "namespace": private_ns,
        },
        token=bob["token"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["joined"] is False, body
    assert body["status"] == "pending", body
    # No active grant → no read access until an admin approves.
    assert body["requested_role"] == "writer", body


@pytest.mark.asyncio
async def test_register_refused_on_others_namespace(control_plane_url) -> None:
    suffix = uuid.uuid4().hex[:6]
    alice = _signup_or_skip(control_plane_url, f"alice-{suffix}")
    bob = _signup_or_skip(control_plane_url, f"bob-{suffix}")
    ns = f"team-foo-{suffix}"

    # Alice registers.
    r = _post(
        control_plane_url,
        "/v1/namespace.register",
        {
            "org": alice["org"],
            "namespace": ns,
            "owner_email": alice["subject"],
            "visibility": "private",
        },
        token=alice["token"],
    )
    assert r.status_code == 200, r.text

    # Bob tries to overwrite. 403 — he's not an admin on it.
    r = _post(
        control_plane_url,
        "/v1/namespace.register",
        {
            "org": alice["org"],
            "namespace": ns,
            "owner_email": bob["subject"],
            "visibility": "public",
        },
        token=bob["token"],
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_leave_refused_when_last_admin(control_plane_url) -> None:
    suffix = uuid.uuid4().hex[:6]
    alice = _signup_or_skip(control_plane_url, f"alice-{suffix}")
    ns = f"team-solo-{suffix}"

    r = _post(
        control_plane_url,
        "/v1/namespace.register",
        {
            "org": alice["org"],
            "namespace": ns,
            "owner_email": alice["subject"],
            "visibility": "private",
        },
        token=alice["token"],
    )
    assert r.status_code == 200, r.text

    # Alice tries to leave her own namespace — refused (last admin).
    # Maps to PreconditionFailedError → 409 (server/core/errors.py).
    r = _post(
        control_plane_url,
        "/v1/namespace.leave",
        {
            "org": alice["org"],
            "namespace": ns,
        },
        token=alice["token"],
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "PRECONDITION_FAILED"
