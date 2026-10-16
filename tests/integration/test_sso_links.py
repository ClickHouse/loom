"""Password accounts carried over to SSO, against a real ClickHouse: an
operator's `loom sso adopt` makes the account the SSO sign-in reaches, with
its own subject and namespace, and `loom sso unlink` takes that back."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from server.auth import login, sso_links
from server.core.ch import get_client, own_write_reads

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def ch() -> Any:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


async def _password_account(ch: Any, org: str, email: str) -> str:
    namespace = f"pw-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": namespace,
                "owner_email": email,
                "contact_email": email,
                "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA",
                "description": "",
                "tags": [],
                "active": True,
                "visibility": "private",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    return namespace


def _sso(*args: str) -> str:
    out = subprocess.run(
        [sys.executable, "-m", "server.cli", "sso", *args],
        env=os.environ,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


async def _resolve(email: str) -> Any:
    with own_write_reads():
        return await login.resolve_identity_for_email(email)


async def test_adopt_makes_the_password_account_the_sso_one(ch: Any) -> None:
    domain = f"adopt-{uuid.uuid4().hex[:6]}.example"
    org, email = f"sso-it-{uuid.uuid4().hex[:6]}", f"lee@{domain}"
    namespace = await _password_account(ch, org, email)
    elsewhere = await _password_account(ch, org, f"lee@other-{domain}")
    assert await _resolve(email) is None  # an unverified address reaches nothing

    assert email in _sso("adopt", "--domain", domain)  # the dry run lists it
    assert await _resolve(email) is None  # and writes nothing
    _sso("adopt", "--domain", domain, "--apply")

    subject, got_org, namespaces, _ = await _resolve(email)
    assert (subject, got_org, namespaces) == (email, org, [namespace])
    with own_write_reads():
        assert await sso_links.unlinked_accounts(email) == []
        left = await sso_links.unlinked_in_domain(f"other-{domain}")
    assert [r["namespace"] for r in left] == [elsewhere]

    _sso("unlink", "--email", email, "--apply")
    assert await _resolve(email) is None


async def test_adopting_an_offboarded_subject_keeps_it_offboarded(ch: Any) -> None:
    from server.auth import revocation

    domain = f"adopt-{uuid.uuid4().hex[:6]}.example"
    org, email = f"sso-it-{uuid.uuid4().hex[:6]}", f"eve@{domain}"
    await _password_account(ch, org, email)
    await revocation.revoke(email, reason="offboarded")
    _sso("adopt", "--domain", domain, "--apply")
    # Adopting expires tokens from before it; the outright revoke stands over that.
    with own_write_reads():
        row = await ch.query_one(
            "SELECT issued_before FROM loom.revoked_tokens FINAL WHERE jti = {j:String}",
            params={"j": email},
        )
    assert row is not None and row["issued_before"] is None
