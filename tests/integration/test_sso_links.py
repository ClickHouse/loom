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
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def ch() -> Any:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


async def _password_account(ch: Any, org: str, email: str, owner: str = "") -> str:
    namespace = f"pw-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": namespace,
                "owner_email": owner or email,
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
        env={**os.environ, "LOOM_OAUTH_LOGIN_PROVIDER": "oidc"},  # adopt waits for SSO
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def _sso_refused(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "server.cli", "sso", *args, "--apply"],
        env={**os.environ, "LOOM_OAUTH_LOGIN_PROVIDER": "oidc"},
        capture_output=True,
        text=True,
        check=False,
    )


async def _resolve(email: str) -> Any:
    with own_write_reads():
        return await login.resolve_identity_for_email(email)


async def test_adopt_makes_the_password_account_the_sso_one(ch: Any) -> None:
    domain = f"adopt-{uuid.uuid4().hex[:6]}.example"
    org, email = unique_org("sso"), f"lee@{domain}"
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
    org, email = unique_org("sso"), f"eve@{domain}"
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


async def test_a_link_skips_an_account_released_since_it_was_found(ch: Any) -> None:
    org, email = unique_org("sso"), f"lee-{uuid.uuid4().hex[:6]}@example.com"
    kept, released = (
        await _password_account(ch, org, email),
        await _password_account(ch, org, email),
    )
    with own_write_reads():
        row = await ch.query_one(
            "SELECT * FROM loom.namespaces FINAL WHERE org = {o:String} AND namespace = {n:String}",
            params={"o": org, "n": released},
        )
    await ch.insert("loom.namespaces", [{**row, "active": False, "contact_email": ""}])
    linked = await sso_links.link_live(email, [(org, kept), (org, released)], "self")
    assert linked == [(org, kept)]
    with own_write_reads():
        assert await sso_links.linked(email) == [(org, kept)]


async def test_link_points_an_account_from_another_address_at_the_provider_one(ch: Any) -> None:
    # Registered with a personal address; the provider knows them by the company one.
    org = unique_org("sso")
    personal, work = f"nik@home-{org}.example", f"nik@corp-{org}.example"
    owner = f"nik-{uuid.uuid4().hex[:6]}"  # revoked_tokens outlives the org purge
    namespace = await _password_account(ch, org, personal, owner=owner)
    assert await _resolve(work) is None

    assert "would sign in as" in _sso(
        "link", "--email", work, "--org", org, "--namespace", namespace
    )
    assert await _resolve(work) is None  # the dry run writes nothing
    _sso("link", "--email", work, "--org", org, "--namespace", namespace, "--apply")

    subject, got_org, namespaces, _ = await _resolve(work)
    assert (subject, got_org, namespaces) == (owner, org, [namespace])  # same subject, same data
    again = _sso_refused("link", "--email", work, "--org", org, "--namespace", namespace)
    assert again.returncode == 2 and "linked to" in again.stderr  # a re-run says so
    with own_write_reads():
        revoked = await ch.query_one(
            "SELECT count() AS n FROM loom.revoked_tokens FINAL WHERE position(jti, {o:String}) > 0",
            params={"o": owner},
        )
    assert revoked["n"] == 0  # its tokens, an agent's included, keep working

    # An address another account names already would leave the sign-in reaching neither.
    other = await _password_account(ch, org, f"lee@corp-{org}.example")
    refused = _sso_refused(
        "link", "--email", f"lee@corp-{org}.example", "--org", org, "--namespace", namespace
    )
    assert refused.returncode == 2 and other in refused.stderr


async def test_link_refuses_an_address_that_still_holds_grants(ch: Any) -> None:
    # The old address was never verified (an unlinked password account's), so
    # its grants admit no one; the link refuses until they are revoked.
    org = unique_org("sso")
    personal = f"kim@home-{org}.example"
    namespace = await _password_account(ch, org, personal, owner="kim")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await ch.insert(
        "loom.namespace_grants",
        [
            {
                "org": org,
                "namespace": "team",
                "subject": personal,
                "role": "writer",
                "granted_at": now,
                "granted_by": "ops",
                "version": 1,
            }
        ],
    )
    refused = _sso_refused(
        "link", "--email", f"kim@corp-{org}.example", "--org", org, "--namespace", namespace
    )
    assert refused.returncode == 2 and "team (writer)" in refused.stderr
    assert "admit no one" in refused.stderr
    assert await _resolve(f"kim@corp-{org}.example") is None


async def _grant(
    ch: Any, org: str, namespace: str, subject: str, *, revoked: bool, status: str = "active"
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await ch.insert(
        "loom.namespace_grants",
        [
            {
                "org": org,
                "namespace": namespace,
                "subject": subject,
                "role": "writer",
                "granted_at": now,
                "granted_by": "ops",
                "revoked_at": now if revoked else None,
                "status": status,
                "version": 1,
            }
        ],
    )


async def test_link_keeps_a_grant_revoked_under_the_old_address_revoked(ch: Any) -> None:
    # The revoked row caps the person's tokens on `secret` through the address;
    # untied, it would cap nothing and the token's claim would stand again.
    org = unique_org("sso")
    personal = f"dee@home-{org}.example"
    namespace = await _password_account(ch, org, personal, owner="dee")
    await _grant(ch, org, "secret", "Dee@Home-" + personal.split("@home-")[1], revoked=True)
    work = f"dee@corp-{org}.example"
    planned = _sso("link", "--email", work, "--org", org, "--namespace", namespace)
    assert "so revoked for dee too: secret" in planned
    done = _sso("link", "--email", work, "--org", org, "--namespace", namespace, "--apply")
    # What it carried, not what it planned (the dry run's "so revoked …" line).
    assert "\n  revoked for dee too: secret\n" in done and "so revoked" not in done
    with own_write_reads():
        carried = await ch.query_one(
            "SELECT role, revoked_at FROM loom.namespace_grants FINAL WHERE org = {org:String} "
            "AND namespace = 'secret' AND subject = 'dee'",
            params={"org": org},
        )
    assert carried is not None and carried["revoked_at"] is not None


async def test_link_finds_an_old_address_grant_whatever_its_case(ch: Any) -> None:
    org = unique_org("sso")
    personal = f"bo@home-{org}.example"
    namespace = await _password_account(ch, org, personal, owner="bo")
    await _grant(ch, org, "team", personal.upper(), revoked=False)
    refused = _sso_refused(
        "link", "--email", f"bo@corp-{org}.example", "--org", org, "--namespace", namespace
    )
    assert refused.returncode == 2 and "team (writer)" in refused.stderr


async def test_link_leaves_the_subjects_own_live_grant_alone(ch: Any) -> None:
    # The address's revoked row never capped the subject's own live grant (the
    # highest live rank across the person's spellings wins), so nothing is carried.
    org = unique_org("sso")
    personal = f"em@home-{org}.example"
    namespace = await _password_account(ch, org, personal, owner="em")
    await _grant(ch, org, "team", "em", revoked=False)
    await _grant(ch, org, "team", personal, revoked=True)
    _sso(
        "link",
        "--email",
        f"em@corp-{org}.example",
        "--org",
        org,
        "--namespace",
        namespace,
        "--apply",
    )
    with own_write_reads():
        mine = await ch.query_all(
            "SELECT revoked_at FROM loom.namespace_grants FINAL WHERE org = {org:String} "
            "AND namespace = 'team' AND lower(subject) = 'em'",
            params={"org": org},
        )
    assert [r["revoked_at"] for r in mine] == [None]


async def test_link_refuses_while_the_old_address_asks_to_join(ch: Any) -> None:
    org = unique_org("sso")
    personal = f"jo@home-{org}.example"
    namespace = await _password_account(ch, org, personal, owner="jo")
    await _grant(ch, org, "team", personal, revoked=False, status="pending")
    refused = _sso_refused(
        "link", "--email", f"jo@corp-{org}.example", "--org", org, "--namespace", namespace
    )
    assert refused.returncode == 2 and "asked to join team" in refused.stderr


async def test_a_link_rebinds_the_addresses_at_once(
    ch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Grants are written under the subject an address resolves to, read from
    # the directory: the new, linked address must start reaching the person,
    # unwaited. This process's alias cache is dropped too. The personal address
    # was never verified (a password account's, unlinked), so it reaches no one.
    from server.auth import aliases
    from server.identity.app import account_admin
    from server.settings import settings

    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    org = unique_org("sso")
    personal, work = f"ash@home-{org}.example", f"ash@corp-{org}.example"
    owner = f"ash-{uuid.uuid4().hex[:6]}"
    namespace = await _password_account(ch, org, personal, owner=owner)
    with own_write_reads():
        assert await aliases.canonical_subject(org, personal) == personal
        assert await aliases.canonical_subject(org, work) == work

    await account_admin.link_account(work, org, namespace, by="op")
    with own_write_reads():
        assert await aliases.canonical_subject(org, work) == owner
        assert await aliases.canonical_subject(org, personal) == personal
        assert set(await aliases.subject_aliases(org, owner)) == {owner, work}
