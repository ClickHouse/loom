"""Session policy failures preserve credentials and trusted email evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from server.auth import authz, device, jit, login, oauth, oauth_store, oidc, revocation, scim
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app
from tests.unit.test_scim_people import people as people

EMAIL = "alice@example.test"
IDENTITY = ("alice", "acme", ["private"], ["writer"])


@pytest.fixture
def session(people, monkeypatch):
    store = MemoryOAuthStore()
    monkeypatch.setattr(oauth, "store", store)
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    people.accounts.append(("acme", "alice", EMAIL, True))
    people.provision(EMAIL)
    minted, rotated, revoked, events = [], [], [], []

    def mint(*args, **kwargs):
        minted.append((args, kwargs))
        return "unit-session-access"

    async def rotate(grant, refresh):
        rotated.append(grant["upstream_refresh"])
        return "unit-upstream-next"

    async def revoke(value):
        revoked.append(value)

    async def not_revoked(*args, **kwargs):
        return False

    async def roles(subject, org, original):
        return original

    async def emit(event, **kwargs):
        events.append((event, kwargs))
        return ""

    monkeypatch.setattr(oauth, "mint_token", mint)
    monkeypatch.setattr(oauth, "_ask_identity_provider", rotate)
    monkeypatch.setattr(oidc, "revoke", revoke)
    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    monkeypatch.setattr(login, "refreshed_roles", roles)
    monkeypatch.setattr(oauth.audit, "emit", emit)
    return {
        "store": store,
        "minted": minted,
        "rotated": rotated,
        "revoked": revoked,
        "events": events,
    }


def grant(**changes):
    return {
        "client_id": "unit-client",
        "redirect_uri": "https://example.test/callback",
        "code_challenge": "challenge",
        "subject": "alice",
        "org": "acme",
        "namespaces": ["private"],
        "roles": ["writer"],
        "exp": int(time.time()) + oauth._REFRESH_TTL,
        "since": int(time.time()),
        "family": "unit-family",
        "upstream_refresh": "unit-upstream-old",
        "verified_email": EMAIL,
        **changes,
    }


async def test_callback_requires_provisioning_before_jit_writes(people, session, monkeypatch):
    async def read_identity(email):
        return None

    async def no_jit(*args, **kwargs):
        pytest.fail("an unprovisioned callback must not finish or create an account")

    monkeypatch.setattr(jit, "finish_unfinished_account", no_jit)
    monkeypatch.setattr(jit, "jit_provision_for_email", no_jit)
    monkeypatch.setattr(login, "resolve_identity_for_email", read_identity)
    result = await oauth._account_for(
        "missing@example.test", {"kind": "dashboard"}, "unit-upstream"
    )
    assert isinstance(result, oauth._Denied)
    assert not session["minted"]


async def test_signed_in_identity_carries_trusted_email_into_consent(session):
    await session["store"].save_client(
        "unit-client", ["https://example.test/callback"], "Unit client"
    )
    pending = {
        "kind": "connect",
        "client_id": "unit-client",
        "redirect_uri": "https://example.test/callback",
        "code_challenge": "challenge",
        "mcp_state": "unit-state",
    }
    await oauth._signed_in_as(pending, IDENTITY, "unit-upstream", EMAIL)
    records = list(session["store"].records("pending").values())
    assert len(records) == 1
    assert records[0]["data"]["verified_email"] == EMAIL


async def test_code_records_keep_the_verified_email(session):
    await oauth._redirect_with_code(
        {
            "client_id": "unit-client",
            "redirect_uri": "https://example.test/callback",
            "code_challenge": "challenge",
        },
        IDENTITY,
        upstream_refresh="unit-upstream",
        state="unit-state",
        since=int(time.time()),
        verified_email=EMAIL,
    )
    records = list(session["store"].records("code").values())
    assert len(records) == 1
    assert records[0]["verified_email"] == EMAIL


async def test_issue_tokens_carries_email_into_the_next_refresh(session):
    await oauth.issue_tokens(grant(), "unit-client", since=int(time.time()))
    saved = list(session["store"].refresh.values())
    assert len(saved) == 1
    assert saved[0]["verified_email"] == EMAIL


async def test_stored_email_is_not_omitted_from_refresh_policy(people, session):
    original = grant(verified_email="missing@example.test")
    await session["store"].save_refresh("unit-refresh", original)
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth._refresh_token_grant(
            {"client_id": "unit-client", "refresh_token": "unit-refresh"}
        )
    assert refused.value.status_code != 503
    assert not session["rotated"] and not session["minted"]
    assert not session["store"].owed


async def test_local_outage_puts_back_identical_refresh_before_upstream_rotation(people, session):
    await session["store"].save_refresh("unit-refresh", grant())
    people.unavailable = True
    with pytest.raises(oauth.OAuthError) as unavailable:
        await oauth._refresh_token_grant(
            {"client_id": "unit-client", "refresh_token": "unit-refresh"}
        )
    assert unavailable.value.status_code == 503
    assert not session["rotated"] and not session["minted"] and not session["revoked"]
    restored = await session["store"].take_refresh("unit-refresh", "unit-client")
    assert restored["family"] == "unit-family"
    assert restored["upstream_refresh"] == "unit-upstream-old"
    assert restored["verified_email"] == EMAIL
    assert not session["store"].owed


async def test_later_outage_keeps_an_already_rotated_upstream_credential(
    people, session, monkeypatch
):
    async def rotated_then_unavailable(current, refresh):
        session["rotated"].append(current["upstream_refresh"])
        authz.forget_ended("identity", "alice")
        people.unavailable = True
        return "unit-upstream-next"

    monkeypatch.setattr(oauth, "_ask_identity_provider", rotated_then_unavailable)
    await session["store"].save_refresh("unit-refresh", grant())
    with pytest.raises(oauth.OAuthError) as unavailable:
        await oauth._refresh_token_grant(
            {"client_id": "unit-client", "refresh_token": "unit-refresh"}
        )
    assert unavailable.value.status_code == 503
    restored = await session["store"].take_refresh("unit-refresh", "unit-client")
    assert restored["upstream_refresh"] == "unit-upstream-next"
    assert restored["verified_email"] == EMAIL and restored["family"] == "unit-family"
    assert not session["minted"] and not session["store"].owed


async def test_n_minus_one_refresh_without_email_uses_current_accounts(session):
    original = grant()
    del original["verified_email"]
    await session["store"].save_refresh("unit-refresh", original)
    result = await oauth._refresh_token_grant(
        {"client_id": "unit-client", "refresh_token": "unit-refresh"}
    )
    assert result["access_token"] == "unit-session-access"


@pytest.mark.parametrize("email", ["", EMAIL])
def test_code_optional_email_round_trip_and_n_minus_one_rewrite(session, email):
    original = grant(verified_email=email)
    encoded = oauth_store._record(original, oauth_store._CODE_FIELDS, spent=False)
    assert encoded["verified_email"] == email
    assert oauth_store._code(encoded)["verified_email"] == email
    legacy = {k: v for k, v in encoded.items() if k != "verified_email"}
    assert oauth_store._code(legacy)["verified_email"] == ""
    assert legacy["nonce"] == encoded["nonce"]


async def test_native_refresh_row_and_parser_keep_the_optional_email(session, monkeypatch):
    row = oauth_store._refresh_row("unit-id", grant(), 1)
    assert row["verified_email"] == EMAIL

    class Rows:
        async def query_one(self, *args, **kwargs):
            return dict(row)

    store = oauth_store.ClickHouseStore()
    monkeypatch.setattr(store, "_ch", lambda: Rows())
    parsed = await store._refresh_grant("unit-id")
    assert parsed["verified_email"] == EMAIL
    del row["verified_email"]
    assert (await store._refresh_grant("unit-id"))["verified_email"] == ""


async def test_refresh_put_back_preserves_held_cas_values_and_email(session):
    await session["store"].save_refresh("unit-refresh", grant())
    held = await session["store"].take_refresh("unit-refresh", "unit-client")
    raw = dict(held["held"])
    await session["store"].save_refresh("unit-refresh", held)
    assert held["verified_email"] == EMAIL
    assert all(isinstance(value, str) for value in raw.values())
    restored = await session["store"].take_refresh("unit-refresh", "unit-client")
    assert restored["verified_email"] == EMAIL and restored["family"] == held["family"]


async def test_device_sign_in_keeps_trusted_email_in_real_cas_record(session):
    user_code = device.normalize_user_code("BCDF-GHJK")
    record = {
        "id": "unit-device",
        "client_id": device.DEVICE_CLIENT_ID,
        "subject": "",
        "org": "",
        "namespaces": [],
        "roles": [],
        "exp": int(time.time()) + 600,
        "family": "",
        "upstream_refresh": "",
        "user_code": device._user_code_id(user_code),
        "status": "pending",
        "address": "unit-address",
        "ticket": "",
    }
    await session["store"].save_device(record)
    response = await device.signed_in(user_code, IDENTITY, "unit-upstream", verified_email=EMAIL)
    assert response.status_code == 200
    assert b"Approve sign-in?" in response.body
    saved = await session["store"].device("unit-device")
    assert saved["verified_email"] == EMAIL
    raw = json.loads(session["store"].keeper["device:unit-device"])
    assert raw["verified_email"] == EMAIL


@pytest.mark.parametrize("outage", [False, True])
async def test_consent_rechecks_before_spending_the_ticket_or_writing_a_code(
    people, session, outage
):
    ticket = "unit-approval"
    pending = {
        "kind": "connect",
        "client_id": "unit-client",
        "redirect_uri": "https://example.test/callback",
        "code_challenge": "challenge",
        "mcp_state": "unit-state",
        "identity": list(IDENTITY),
        "verified_email": EMAIL,
        "since": int(time.time()),
        "upstream_refresh": oauth.seal(oauth.UPSTREAM_PURPOSE, "unit-upstream"),
    }
    await session["store"].save_pending(oauth_store.token_id(ticket), pending)
    people.users.clear()
    people.unavailable = outage
    response = TestClient(oauth_app()).post(
        "/oauth/authorize/approve",
        data={"ticket": ticket, "decision": "approve"},
        follow_redirects=False,
    )
    assert response.status_code == (503 if outage else 400)
    assert await session["store"].pending(oauth_store.token_id(ticket)) == pending
    assert not session["store"].records("code") and not session["minted"]
    if outage:
        assert response.headers["Retry-After"] == "5"


async def test_code_policy_outage_restores_the_same_code_and_verified_email(people, session):
    verifier = "unit-code-verifier" * 3
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    original = grant(code_challenge=challenge)
    await session["store"].save_code("unit-code", original)
    people.unavailable = True
    with pytest.raises(oauth.OAuthError) as unavailable:
        await oauth._authorization_code_grant(
            {
                "client_id": "unit-client",
                "code": "unit-code",
                "code_verifier": verifier,
                "redirect_uri": original["redirect_uri"],
            }
        )
    assert unavailable.value.status_code == 503
    restored = await session["store"].take_code("unit-code", "unit-client", lambda _: True)
    assert restored["verified_email"] == EMAIL
    assert restored["upstream_refresh"] == original["upstream_refresh"]
    assert restored["family"] == original["family"]
    assert not session["minted"] and not session["store"].owed


def approved_device(*, status="approved", ticket="unit-approval"):
    return {
        "id": oauth_store.token_id("unit-device-code"),
        "client_id": device.DEVICE_CLIENT_ID,
        "subject": "alice",
        "org": "acme",
        "namespaces": ["private"],
        "roles": ["writer"],
        "exp": int(time.time()) + 600,
        "family": "unit-family",
        "upstream_refresh": "unit-upstream",
        "verified_email": EMAIL,
        "user_code": device._user_code_id("BCDFGHJK"),
        "status": status,
        "address": "unit-address",
        "ticket": oauth_store.token_id(ticket),
    }


@pytest.mark.parametrize("outage", [False, True])
async def test_device_approval_rechecks_before_live_grant_and_keeps_retryable_state(
    people, session, outage
):
    original = approved_device(status="pending")
    await session["store"].save_device(original)
    before = session["store"].keeper["device:" + original["id"]]
    people.users.clear()
    people.unavailable = outage
    response = TestClient(oauth_app()).post(
        "/oauth/device/approve",
        data={"user_code": "BCDFGHJK", "ticket": "unit-approval", "decision": "approve"},
    )
    assert response.status_code == (503 if outage else 200)
    saved = await session["store"].device(original["id"])
    assert saved["status"] == ("pending" if outage else "denied")
    assert not session["minted"]
    if outage:
        assert response.headers["Retry-After"] == "5"
        assert session["store"].keeper["device:" + original["id"]] == before
        assert not session["revoked"]
    else:
        assert saved["family"] == "" and saved["upstream_refresh"] == ""


async def test_device_grant_outage_puts_back_same_approved_cas_record(people, session):
    original = approved_device()
    await session["store"].save_device(original)
    people.unavailable = True
    with pytest.raises(oauth.OAuthError) as unavailable:
        await device.device_code_grant(
            {"client_id": device.DEVICE_CLIENT_ID, "device_code": "unit-device-code"}
        )
    assert unavailable.value.status_code == 503
    restored = await session["store"].take_device("unit-device-code")
    assert restored["status"] == "approved"
    assert restored["verified_email"] == EMAIL
    assert restored["family"] == original["family"]
    assert restored["upstream_refresh"] == original["upstream_refresh"]
    assert not session["minted"] and not session["revoked"]


@pytest.mark.parametrize("kind", ["code", "device"])
async def test_previous_writer_drops_email_proof_without_changing_raw_cas_ownership(
    people, session, kind
):
    if kind == "code":
        await session["store"].save_code("unit-code", grant())
        key = "code:" + oauth_store.token_id("unit-code")
    else:
        original = approved_device()
        await session["store"].save_device(original)
        key = "device:" + original["id"]
    old = session["store"].keeper[key]
    legacy = {name: value for name, value in json.loads(old).items() if name != "verified_email"}
    legacy["nonce"] = "unit-previous-writer-nonce"
    rewritten = json.dumps(legacy)
    assert await session["store"]._swap(key, old, rewritten, int(legacy["exp"])) == rewritten
    if kind == "code":
        parsed = await session["store"].take_code("unit-code", "unit-client", lambda _: True)
    else:
        parsed = await session["store"].take_device("unit-device-code")
    assert parsed["verified_email"] == ""
    assert list(parsed["held"].keys()) == [key]
    assert json.loads(parsed["held"][key])["spent"] is True
    people.accounts[:] = [("acme", "alice", EMAIL, False)]
    assert await scim.refuses_subject(
        parsed["subject"], org=parsed["org"], verified_email=parsed["verified_email"]
    )
    assert not session["minted"]
