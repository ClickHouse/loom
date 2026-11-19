"""Verified reader evidence follows real account writes and link versions."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from tests.integration._resources import unique_org


async def test_reader_evidence_follows_account_and_link_lifecycle(monkeypatch) -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.auth import jit, reader_identity, self_signup, sso_links
    from server.core.ch import get_client, set_query_tag
    from server.settings import settings

    ch = get_client()
    set_query_tag("test.mirror_reader_evidence")
    org = unique_org("reader-evidence")
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example/")
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    try:
        await ch.connect()
        # The password signup path hashes even a claimed address before writing it.
        await self_signup._claim_account(
            self_signup.SelfSignupInput(
                namespace="password-owner",
                email="claimed@example.com",
                password="correct-horse-battery",
            ),
            org,
            "password-owner",
        )
        password_row = await ch.query_one(
            "SELECT * FROM loom.namespaces FINAL WHERE org = {org:String} AND namespace = 'password-owner'",
            {"org": org},
        )
        assert password_row["password_hash"].startswith("$argon2")
        assert password_row["contact_email"] == "claimed@example.com"
        reader = await reader_identity.resolve_reader(org, "password-owner", ch=ch)
        assert reader == reader_identity.ReaderIdentity("password-owner", frozenset(), frozenset())
        bare = await reader_identity.resolve_reader(org, "claimed@example.com", ch=ch)
        assert not bare.emails and not bare.domains

        await sso_links.link("claimed@example.com", [(org, "password-owner")], "operator")
        reader = await reader_identity.resolve_reader(org, "password-owner", ch=ch)
        assert reader.emails == frozenset({"claimed@example.com"}) and not reader.domains
        await sso_links.attest_domain(
            "claimed@example.com",
            org,
            "password-owner",
            {"iss": "https://idp.example", "hd": "example.com"},
        )
        assert not (await reader_identity.resolve_reader(org, "password-owner", ch=ch)).domains
        await sso_links.unlink("claimed@example.com", [(org, "password-owner")], "operator")
        assert not (await reader_identity.resolve_reader(org, "password-owner", ch=ch)).emails

        # JIT's actual provisioning path writes the verified passwordless contact.
        identity = await jit._provision_jit(org, "signed@example.com", "u-signed")
        assert identity is not None
        owner = identity[0]
        reader = await reader_identity.resolve_reader(org, "signed@example.com", ch=ch)
        assert reader.subject == owner and reader.emails == frozenset({"signed@example.com"})
        assert not reader.domains
        await sso_links.attest_domain(
            "signed@example.com", org, owner, {"iss": "https://idp.example", "hd": "Example.COM"}
        )
        rows = await ch.query_all(
            "SELECT * FROM loom.sso_links FINAL WHERE org = {org:String} AND email = 'signed@example.com'",
            {"org": org},
        )
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == {
            "email",
            "org",
            "namespace",
            "linked",
            "linked_by",
            "linked_at",
            "version",
            "hosted_domain",
            "issuer",
        }
        assert row == {
            "email": "signed@example.com",
            "org": org,
            "namespace": owner,
            "linked": 1,
            "linked_by": owner,
            "linked_at": row["linked_at"],
            "version": row["version"],
            "hosted_domain": "example.com",
            "issuer": "https://idp.example",
        }
        assert isinstance(row["linked_at"], datetime) and int(row["version"]) > 0
        assert (await reader_identity.resolve_reader(org, owner, ch=ch)).domains == frozenset(
            {"example.com"}
        )
        monkeypatch.setattr(settings, "oidc_issuer", "https://other.example")
        assert not (await reader_identity.resolve_reader(org, owner, ch=ch)).domains
        monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example/")
        assert not (
            await reader_identity.resolve_reader(unique_org("other-reader"), owner, ch=ch)
        ).emails
        await sso_links.attest_domain(
            "signed@example.com", org, owner, {"iss": "https://idp.example"}
        )
        assert not (await reader_identity.resolve_reader(org, owner, ch=ch)).domains
        await sso_links.attest_domain(
            "signed@example.com", org, owner, {"iss": "https://idp.example", "hd": "example.com"}
        )
        await sso_links.unlink("signed@example.com", [(org, owner)], "operator")
        assert not (await reader_identity.resolve_reader(org, owner, ch=ch)).domains

        # A release/reassignment changes the full account row; old link evidence remains dark.
        account = await ch.query_one(
            "SELECT * FROM loom.namespaces FINAL WHERE org = {org:String} AND namespace = {ns:String}",
            {"org": org, "ns": owner},
        )
        await ch.insert(
            "loom.namespaces",
            [
                {
                    **account,
                    "active": False,
                    "owner_email": "",
                    "contact_email": "",
                    "updated_at": datetime.now(UTC),
                }
            ],
        )
        await sso_links.attest_domain(
            "signed@example.com", org, owner, {"iss": "https://idp.example", "hd": "example.com"}
        )
        assert not (await reader_identity.resolve_reader(org, owner, ch=ch)).emails
        stored = await ch.query_one(
            "SELECT * FROM loom.namespaces FINAL WHERE org = {org:String} AND namespace = {ns:String}",
            {"org": org, "ns": owner},
        )
        assert {
            k: stored[k]
            for k in account
            if k not in {"active", "owner_email", "contact_email", "updated_at"}
        } == {
            k: v
            for k, v in account.items()
            if k not in {"active", "owner_email", "contact_email", "updated_at"}
        }
        assert stored["active"] == 0 and stored["owner_email"] == stored["contact_email"] == ""
    finally:
        await ch.disconnect()


async def test_missing_domain_clears_every_account_before_reader_count_shrinks(monkeypatch) -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.auth import reader_identity, sso_links
    from server.core.ch import get_client, set_query_tag
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row
    from server.settings import settings

    ch = get_client()
    set_query_tag("test.mirror_reader_many_accounts")
    org = unique_org("reader-many")
    owner, email = "many-reader", "many@example.com"
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example")
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    try:
        await ch.connect()
        now = datetime.now(UTC)
        rows = [
            {
                **registry_row(
                    NamespaceRegisterInput(org=org, namespace=f"member-{i:03}", owner_email=owner),
                    None,
                    now,
                ),
                "contact_email": email,
                "password_hash": "",
            }
            for i in range(129)
        ]
        await ch.insert("loom.namespaces", rows)
        await sso_links._write(
            email,
            [(org, r["namespace"]) for r in rows],
            owner,
            linked=1,
            hosted_domain="example.com",
            issuer="https://idp.example",
        )
        assert await reader_identity.resolve_reader(org, owner, ch=ch) is None
        await sso_links.attest_domain(email, org, owner, {"iss": "https://idp.example"})
        await ch.insert(
            "loom.namespaces",
            [{**r, "active": False, "updated_at": datetime.now(UTC)} for r in rows[1:]],
        )
        reader = await reader_identity.resolve_reader(org, owner, ch=ch)
        assert reader.emails == frozenset({email})
        assert not reader.domains
        saved = await ch.query_all(
            "SELECT hosted_domain, issuer, linked FROM loom.sso_links FINAL WHERE org = {org:String}",
            {"org": org},
        )
        assert len(saved) == 129
        assert all(
            r == {"hosted_domain": "", "issuer": "https://idp.example", "linked": 1} for r in saved
        )
    finally:
        await ch.disconnect()
