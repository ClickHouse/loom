"""Verified sign-in evidence survives real refresh and Keeper transitions."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.auth import claims, oauth, oauth_store
from server.auth.login import SignInUnavailable
from server.auth.oauth_store import ClickHouseStore, Device, Grant, token_id
from server.core.secrets_at_rest import unseal
from server.identity.app.claim_release import release_claim
from server.settings import settings
from tests.integration import scim_session_previous as previous
from tests.integration.test_scim_person_live_ch import People
from tests.integration.test_scim_person_live_ch import people as people

pytestmark = pytest.mark.usefixtures("ch_target_required")


class Sessions(ClickHouseStore):
    def __init__(self, people: People):
        self.people = people
        self.keys: set[str] = set()
        self.families: set[str] = set()

    async def _swap(self, key: str, old: str | None, new: str, expires_at: int) -> str | None:
        self.keys.add(key)
        if key.startswith("family:"):
            self.families.add(key.removeprefix("family:"))
        return await super()._swap(key, old, new, expires_at)

    def grant(self, *, email: str = "") -> Grant:
        return {
            "client_id": "client-" + uuid.uuid4().hex,
            "redirect_uri": "",
            "code_challenge": "",
            "subject": "owner",
            "org": self.people.org,
            "namespaces": ["account"],
            "roles": ["writer"],
            "exp": int(time.time()) + 600,
            "since": int(time.time()),
            "family": uuid.uuid4().hex,
            "upstream_refresh": "",
            "verified_email": email,
        }

    def make_device(self, grant: Grant) -> Device:
        return {
            "id": token_id(uuid.uuid4().hex),
            "client_id": grant["client_id"],
            "subject": grant["subject"],
            "org": grant["org"],
            "namespaces": list(grant["namespaces"]),
            "roles": list(grant["roles"]),
            "exp": grant["exp"],
            "family": grant["family"],
            "upstream_refresh": grant["upstream_refresh"],
            "verified_email": grant.get("verified_email", ""),
            "user_code": token_id(uuid.uuid4().hex),
            "status": "approved",
            "address": "127.0.0.1",
            "ticket": token_id(uuid.uuid4().hex),
        }


@pytest.fixture
async def sessions(people: People, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sessions]:
    store = Sessions(people)
    monkeypatch.setattr(oauth, "store", store)
    monkeypatch.setattr(settings, "jwt_secret", "unit-native-session-sealing-root")
    monkeypatch.setattr(settings, "oauth_login_provider", "")
    try:
        yield store
    finally:
        first_error: BaseException | None = None
        for key in store.keys:
            try:
                current = await claims.get(people.client, key)
                if current is not None:
                    await claims._drop(people.client, key, current)
            except BaseException as exc:  # noqa: BLE001 — attempt every owned key
                if first_error is None:
                    first_error = exc
        try:
            if store.families:
                await people.client.execute(
                    "ALTER TABLE loom.revoked_tokens DELETE WHERE jti IN {ids:Array(String)} "
                    "SETTINGS mutations_sync = 2",
                    params={"ids": list(store.families)},
                )
        except BaseException as exc:  # noqa: BLE001 — preserve the first cleanup failure
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


async def test_real_refresh_round_trip_putback_and_rotation_preserve_email(
    sessions: Sessions,
) -> None:
    email = sessions.people.email("session")
    grant = sessions.grant(email=email)
    grant["upstream_refresh"] = "synthetic-upstream-before"
    token = uuid.uuid4().hex
    await sessions.save_refresh(token, grant)
    row = await sessions.people.client.query_one(
        "SELECT verified_email, upstream_refresh FROM loom.oauth_state FINAL "
        "WHERE record_type = 'session_refresh' AND id = {id:String}",
        params={"id": token_id(token)},
    )
    assert row is not None and row["verified_email"] == email
    assert row["upstream_refresh"] != grant["upstream_refresh"]
    assert (
        unseal(oauth_store.UPSTREAM_PURPOSE, row["upstream_refresh"]) == grant["upstream_refresh"]
    )
    spent = await sessions.take_refresh(token, grant["client_id"])
    assert spent is not None and spent["verified_email"] == email
    key = f"family:{grant['family']}"
    held = spent["held"][key]
    assert await claims.get(sessions.people.client, key) == held
    spent["upstream_refresh"] = "synthetic-upstream-rotated"
    await sessions.save_refresh(token, spent)
    retried = await sessions.take_refresh(token, grant["client_id"])
    assert retried is not None and retried["family"] == grant["family"]
    assert retried["verified_email"] == email
    assert retried["upstream_refresh"] == "synthetic-upstream-rotated"
    replacement = uuid.uuid4().hex
    await sessions.save_refresh(replacement, {**retried, "generation": 1})
    rotated = await sessions.take_refresh(replacement, grant["client_id"])
    assert rotated is not None and rotated["generation"] == 1
    assert rotated["verified_email"] == email
    assert rotated["family"] == grant["family"]
    with pytest.raises(oauth_store.CodeReplayed):
        await sessions.take_refresh(token, grant["client_id"])


@pytest.mark.parametrize("kind", ["code", "device"])
async def test_real_keeper_records_preserve_email_and_exact_held_cas(
    sessions: Sessions, kind: str
) -> None:
    email = sessions.people.email("keeper")
    grant = sessions.grant(email=email)
    credential = uuid.uuid4().hex
    if kind == "code":
        await sessions.save_code(credential, grant)
        key = f"code:{token_id(credential)}"
        spent = await sessions.take_code(credential, grant["client_id"], lambda _: True)
    else:
        device = sessions.make_device(grant)
        device["id"] = token_id(credential)
        await sessions.save_device(device)
        key = f"device:{device['id']}"
        spent = await sessions.take_device(credential)
    assert spent is not None and spent["verified_email"] == email
    held = spent["held"][key]
    assert await claims.get(sessions.people.client, key) == held
    assert json.loads(held)["verified_email"] == email
    if kind == "code":
        await sessions.save_code(credential, spent)  # type: ignore[arg-type]
    else:
        await sessions.save_device(spent)  # type: ignore[arg-type]
    restored = await claims.get(sessions.people.client, key)
    assert restored is not None and restored != held
    assert json.loads(restored)["verified_email"] == email
    assert json.loads(restored)["spent"] is False
    with pytest.raises(claims.Changed):
        await sessions._put(key, held, json.loads(held))


@pytest.mark.parametrize("kind", ["code", "device"])
async def test_previous_keeper_writer_drops_email_without_adding_proof(
    sessions: Sessions, kind: str
) -> None:
    email = sessions.people.email("previous")
    grant = sessions.grant(email=email)
    if kind == "code":
        fields = previous._CODE_FIELDS
        record = oauth_store._record(grant, fields, spent=False)
        old_read = previous._code(record)
        current_read = oauth_store._code
    else:
        device = sessions.make_device(grant)
        fields = previous._DEVICE_FIELDS
        record = oauth_store._record(device, fields, spent=False)
        old_read = previous._device(record)
        current_read = oauth_store._device
    assert "verified_email" not in old_read
    old_write = previous._record(old_read, fields, spent=False)
    assert "verified_email" not in old_write
    key = f"{kind}:{uuid.uuid4().hex}"
    held = await sessions._put(key, None, record)
    rewritten = await sessions._put(key, held, old_write)
    assert await claims.get(sessions.people.client, key) == rewritten
    parsed = current_read(json.loads(rewritten))
    assert parsed["verified_email"] == ""
    assert parsed["subject"] == grant["subject"]
    await sessions.people.provision(email)
    assert await sessions.people.refused(parsed["subject"], verified_email=parsed["verified_email"])
    await sessions.people.account(grant["subject"], email)
    assert not await sessions.people.refused(
        parsed["subject"], verified_email=parsed["verified_email"]
    )


async def test_previous_refresh_writer_uses_default_empty_and_cannot_borrow_a_stored_email(
    sessions: Sessions,
) -> None:
    email = sessions.people.email("previous-refresh")
    grant = sessions.grant(email=email)
    token = uuid.uuid4().hex
    await sessions.save_refresh(token, grant)
    row = previous._refresh_row(token_id(token), grant, time.time_ns())
    assert "verified_email" not in row
    row["version"] += 1
    await sessions.people.client.insert("loom.oauth_state", [row])
    loaded = await sessions.take_refresh(token, grant["client_id"])
    assert loaded is not None and loaded["verified_email"] == ""
    assert loaded["family"] == grant["family"]
    assert loaded["held"][f"family:{grant['family']}"] == await claims.get(
        sessions.people.client, f"family:{grant['family']}"
    )
    await sessions.people.provision(email)
    assert await sessions.people.refused(loaded["subject"], verified_email=loaded["verified_email"])


async def test_released_account_cannot_refresh_with_its_stored_verified_email(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = sessions.people.email("released")
    grant = sessions.grant(email=email)
    namespace = await sessions.people.account(grant["subject"], email)
    await sessions.people.provision(email)
    token = uuid.uuid4().hex
    await sessions.save_refresh(token, grant)
    assert not await sessions.people.refused(grant["subject"], verified_email=email)
    released = await release_claim(
        org=grant["org"], namespace=namespace, reason="test", actor_subject="host"
    )
    assert released is not None and released.released

    async def forbidden_mint(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a released account reached token issuance")

    monkeypatch.setattr(oauth, "issue_tokens", forbidden_mint)
    with pytest.raises(oauth.OAuthError) as refused:
        await oauth._refresh_token_grant({"client_id": grant["client_id"], "refresh_token": token})
    assert refused.value.status_code == 400
    with pytest.raises(oauth_store.SessionRetired):
        await sessions.take_refresh(token, grant["client_id"])


async def test_native_local_policy_outage_puts_back_the_same_refresh_without_rotation(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = sessions.people.email("outage")
    grant = sessions.grant(email=email)
    grant["upstream_refresh"] = "synthetic-upstream-current"
    token = uuid.uuid4().hex
    await sessions.save_refresh(token, grant)
    await sessions.people.account(grant["subject"], email)
    original = sessions.people.client.query_one
    outage = OSError("synthetic local policy read failure")

    async def failed_policy(sql: str, *args: Any, **kwargs: Any) -> Any:
        if "loom.namespaces" in sql:
            raise outage
        return await original(sql, *args, **kwargs)

    async def forbidden_upstream(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("local policy outage reached the identity provider")

    monkeypatch.setattr(sessions.people.client, "query_one", failed_policy)
    monkeypatch.setattr(oauth, "_ask_identity_provider", forbidden_upstream)
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    with pytest.raises(oauth.OAuthError) as unavailable:
        await oauth._refresh_token_grant({"client_id": grant["client_id"], "refresh_token": token})
    assert unavailable.value.status_code == 503
    assert isinstance(unavailable.value.__cause__, SignInUnavailable)
    assert unavailable.value.__cause__.__cause__ is outage
    retried = await sessions.take_refresh(token, grant["client_id"])
    assert retried is not None and retried["family"] == grant["family"]
    assert retried["verified_email"] == email
    assert retried["upstream_refresh"] == grant["upstream_refresh"]
