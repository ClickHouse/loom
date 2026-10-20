"""A namespace's profile schema and each user's structured profile, end to end:
an admin sets the schema, a worker pass builds the profile from the user's
memories with the fake model, and a read returns it with its status.

Runs against a real Control Plane + ClickHouse (LOOM_BASE_URL, LOOM_CH_*); the
worker pass runs in this process against the same ClickHouse.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

ORG = unique_org("structprofile")

SCHEMA = {
    "type": "object",
    "properties": {
        "timezone": {"type": "string", "description": "the user's time zone"},
        "languages": {"type": "array", "items": {"type": "string"}, "description": "languages"},
    },
}


def _post(base: str, token: str, op: str, body: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=30.0) as c:
        return c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})


def _ok(r: httpx.Response) -> dict[str, Any]:
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


@pytest.fixture(scope="module", autouse=True)
async def _ch_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


@pytest.fixture
def ns(control_plane_url: str, mint_test_token):
    """The test's own namespace; any schema it set is cleared afterwards, so later
    passes on this ClickHouse never reach it."""
    name = f"structured-profile-{os.urandom(3).hex()}"
    yield name
    token = mint_test_token(org=ORG, namespaces=[name], roles=["admin"], subject="op")
    _ok(_post(control_plane_url, token, "profile_schema.clear", {"org": ORG, "namespace": name}))


@pytest.fixture
def admin(mint_test_token, ns: str) -> str:
    return mint_test_token(org=ORG, namespaces=[ns], roles=["admin", "writer"], subject="op")


def _remember(base: str, token: str, ns: str, user: str, key: str, value: str, **extra: Any) -> str:
    body = {"org": ORG, "namespace": ns, "agent": "bot", "user_id": user}
    out = _ok(
        _post(base, token, "memory.set", {**body, "memory_key": key, "value": value, **extra})
    )
    return str(out["memory_id"])


def _read(base: str, token: str, ns: str, user: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"org": ORG, "namespace": ns, "agent": "bot"}
    if user is not None:
        body["user_id"] = user
    return _ok(_post(base, token, "profile.structured", body))


async def _build(ns: str) -> None:
    """One worker pass over the test's namespace only, with a fresh scan: the worker
    keeps a namespace's last scan for minutes, longer than a test waits."""
    from server.memory.workers import structured_profile
    from server.memory.workers.structured_profile import StructuredProfileConfig, run_pass

    structured_profile._scans.pop((ORG, ns), None)
    cfg = StructuredProfileConfig(cooldown_seconds=0, max_users_per_pass=50, only=(ORG, ns))
    result = await run_pass(cfg)
    assert result.errors == 0


@pytest.mark.asyncio
async def test_a_profile_is_built_in_the_schema_shape_and_kept_current(
    control_plane_url: str, admin: str, ns: str, ch_client
) -> None:
    base = control_plane_url
    assert _read(base, admin, ns, "u1")["status"] == "no_schema"

    out = _ok(
        _post(
            base, admin, "profile_schema.set", {"org": ORG, "namespace": ns, "json_schema": SCHEMA}
        )
    )
    assert out["found"] and out["json_schema"] == SCHEMA and len(out["schema_sha256"]) == 64
    got = _ok(_post(base, admin, "profile_schema.get", {"org": ORG, "namespace": ns}))
    assert got["schema_sha256"] == out["schema_sha256"] and got["updated_by"] == "process:op"

    tz = _remember(base, admin, ns, "u1", "u1_tz", "My timezone is Europe/Berlin.")
    lang = _remember(
        base, admin, ns, "u1", "u1_lang", "I speak these languages: English and German."
    )
    # A high row is never shown to the model: the profile keeps the stated zone.
    _remember(base, admin, ns, "u1", "u1_secret", "My timezone is Atlantis.", sensitivity="high")
    _remember(base, admin, ns, "u2", "u2_tz", "My timezone is Asia/Tokyo.")

    pending = _read(base, admin, ns, "u1")
    assert pending["status"] == "pending"
    assert pending["profile"] == {"timezone": None, "languages": None}

    await _build(ns)
    ready = _read(base, admin, ns, "u1")
    assert ready["status"] == "ready", ready
    assert ready["profile"] == {
        "timezone": "My timezone is Europe/Berlin",
        "languages": ["I speak these languages: English and German"],
    }
    assert ready["built_at"] and ready["memory_id"] and ready["version"]
    assert ready["schema_sha256"] == out["schema_sha256"]

    # The row: a derived profile row naming the memories it came from, high one excluded.
    row = ch_client.query(
        "SELECT memory_type, agent, source_class, source_trace, value FROM loom.memory FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND user_id = 'u1' "
        "AND memory_key = 'structured_profile' AND tombstoned_at IS NULL",
        parameters={"o": ORG, "n": ns},
    ).named_results()
    rows = list(row)
    assert len(rows) == 1
    assert rows[0]["memory_type"] == "profile" and rows[0]["agent"] == "-"
    assert rows[0]["source_class"] == "derived"
    trace = rows[0]["source_trace"]
    assert trace.startswith("profile.fold:structured_profile:")
    assert sorted(trace.rsplit(":", 1)[1].split(",")) == sorted([tz, lang])
    assert json.loads(rows[0]["value"]) == ready["profile"]

    # A new fact puts the profile behind its memories until the next pass.
    _remember(base, admin, ns, "u1", "u1_tz", "My timezone is America/New_York.")
    assert _read(base, admin, ns, "u1")["status"] == "stale"
    await _build(ns)
    again = _read(base, admin, ns, "u1")
    assert again["status"] == "ready"
    assert again["profile"]["timezone"] == "My timezone is America/New_York"
    assert again["version"] > ready["version"]

    # A schema change marks it stale, served in the new shape until it is rebuilt.
    changed = {
        "type": "object",
        "properties": {
            "timezone": {"type": "string", "description": "the user's time zone"},
            "pronouns": {"type": "string", "description": "pronouns"},
        },
    }
    _ok(
        _post(
            base, admin, "profile_schema.set", {"org": ORG, "namespace": ns, "json_schema": changed}
        )
    )
    stale = _read(base, admin, ns, "u1")
    assert stale["status"] == "stale"
    assert stale["profile"] == {"timezone": "My timezone is America/New_York", "pronouns": None}
    await _build(ns)
    assert _read(base, admin, ns, "u1")["status"] == "ready"

    cleared = _ok(_post(base, admin, "profile_schema.clear", {"org": ORG, "namespace": ns}))
    assert cleared["found"] and cleared["json_schema"] == changed
    assert _read(base, admin, ns, "u1")["status"] == "no_schema"
    gone = _ok(_post(base, admin, "profile_schema.get", {"org": ORG, "namespace": ns}))
    assert gone["found"] is False


def test_a_bad_schema_is_refused(control_plane_url: str, admin: str, ns: str) -> None:
    bad = {"type": "object", "properties": {"timezone": {"type": "string"}}}
    r = _post(
        control_plane_url,
        admin,
        "profile_schema.set",
        {"org": ORG, "namespace": ns, "json_schema": bad},
    )
    assert r.status_code == 422 and "description" in r.text


def test_setting_a_schema_needs_admin(control_plane_url: str, mint_test_token, ns: str) -> None:
    writer = mint_test_token(org=ORG, namespaces=[ns], roles=["writer"])
    r = _post(
        control_plane_url,
        writer,
        "profile_schema.set",
        {"org": ORG, "namespace": ns, "json_schema": SCHEMA},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_token_pinned_to_a_user_reads_only_that_users_profile(
    control_plane_url: str, admin: str, ns: str
) -> None:
    from server.auth.jwt import mint_token

    base = control_plane_url
    _ok(
        _post(
            base, admin, "profile_schema.set", {"org": ORG, "namespace": ns, "json_schema": SCHEMA}
        )
    )
    _remember(base, admin, ns, "alice", "alice_tz", "My timezone is Europe/Paris.")
    _remember(base, admin, ns, "bob", "bob_tz", "My timezone is Europe/Rome.")
    await _build(ns)

    alice = mint_token(
        subject="alice-app",
        org=ORG,
        namespaces=[ns],
        roles=["viewer"],
        read_user_id="alice",
        ttl_seconds=600,
    )
    mine = _read(base, alice, ns, None)  # the pin names the user
    assert mine["status"] == "ready"
    assert mine["profile"]["timezone"] == "My timezone is Europe/Paris"
    assert _read(base, alice, ns, "alice") == mine
    r = _post(
        base,
        alice,
        "profile.structured",
        {"org": ORG, "namespace": ns, "agent": "bot", "user_id": "bob"},
    )
    assert r.status_code == 403
    assert "Rome" not in r.text

    # A namespace-wide reader names the user; with none named there is no profile to read.
    r = _post(base, admin, "profile.structured", {"org": ORG, "namespace": ns, "agent": "bot"})
    assert r.status_code == 422


@pytest.mark.slow
@pytest.mark.asyncio
async def test_erasing_a_user_removes_their_profile(
    control_plane_url: str, admin: str, ns: str, mint_test_token, ch_client
) -> None:
    from loom import Agent

    base = control_plane_url
    body = {"org": ORG, "namespace": ns, "json_schema": SCHEMA}
    _ok(_post(base, admin, "profile_schema.set", body))
    _remember(base, admin, ns, "carol", "carol_tz", "My timezone is Europe/Madrid.")
    await _build(ns)
    assert _read(base, admin, ns, "carol")["status"] == "ready"

    org_admin = mint_test_token(org=ORG, namespaces=["*"], roles=["org_admin", "admin", "writer"])
    async with Agent(
        org=ORG, namespace=ns, agent="ops", base_url=base, timeout_s=60.0, api_key=org_admin
    ) as a:
        result = await a.erasure.request_user_id("carol", requester="ops@example.com")
    assert result["status"] == "completed"

    live = ch_client.query(
        "SELECT count() FROM loom.memory FINAL WHERE org = {o:String} AND namespace = {n:String} "
        "AND user_id = 'carol' AND tombstoned_at IS NULL",
        parameters={"o": ORG, "n": ns},
    ).result_rows[0][0]
    assert live == 0
    assert _read(base, admin, ns, "carol")["status"] == "pending"


def test_a_session_bounded_token_cannot_read_a_profile(control_plane_url: str, ns: str) -> None:
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="chat",
        org=ORG,
        namespaces=[ns],
        roles=["viewer"],
        read_scope="own_session",
        ttl_seconds=600,
    )
    body = {"org": ORG, "namespace": ns, "agent": "bot", "user_id": "u1", "session_id": "s1"}
    r = _post(control_plane_url, token, "profile.structured", body)
    assert r.status_code == 403 and "sessions" in r.text
