"""An account's email is another spelling of its owner only once it is verified, on
clickhouse-local against the real tables: an SSO account's (no password; the identity
provider vouched for it), or a password account linked to it in `loom.sso_links`. A
self-signup email is unverified, so whoever typed someone else's address first holds
none of the grants written under it: not at the gate, not in the alias reads, not in
`grants.list`'s fold."""

from __future__ import annotations

from typing import Any

import pytest

from server.auth import aliases
from server.core import grant_cache
from server.identity.store import grants
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = [
    pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed"),
    pytest.mark.claim_cap,  # the grant map reads real rows here
]

ORG = "acme"


def _account(namespace: str, email: str, password_hash: str) -> str:
    return (
        "INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email,"
        " password_hash, active, created_at, updated_at) VALUES"
        f" ('{ORG}', '{namespace}', '{namespace}', '{email}', '{password_hash}', 1,"
        " now(), now())"
    )


def _grant(namespace: str, subject: str) -> str:
    return (
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, status,"
        " granted_at, granted_by, version) VALUES"
        f" ('{ORG}', '{namespace}', '{subject}', 'writer', 'active', now(), 'admin', 1)"
    )


SETUP = (
    ddl("namespaces"),
    ddl("sso_links"),
    ddl("namespace_grants"),
    # Self-signed up under another person's address: unverified.
    _account("mallory", "bob@corp.example", "argon2-hash"),
    # An SSO account: the identity provider verified its email.
    _account("u-carol", "carol@corp.example", ""),
    # A password account linked to its email.
    _account("dave", "dave@corp.example", "argon2-hash"),
    "INSERT INTO loom.sso_links (email, org, namespace, linked, linked_by, linked_at, version)"
    f" VALUES ('dave@corp.example', '{ORG}', 'dave', 1, 'op', now(), 1)",
    # Each address granted before its person signed in, and each handle on team-x.
    *(
        _grant(ns, f"{who}@corp.example")
        for ns in ("team-x", "team-y")
        for who in ("bob", "carol", "dave")
    ),
    *(_grant("team-x", handle) for handle in ("mallory", "u-carol", "dave")),
)


class _CH(LocalCH):
    async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    client = _CH(*SETUP)
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "spellings"),
    [
        ("mallory", ["mallory"]),
        ("bob@corp.example", ["bob@corp.example"]),
        ("u-carol", ["u-carol", "carol@corp.example"]),
        ("dave", ["dave", "dave@corp.example"]),
    ],
)
async def test_only_a_verified_email_is_a_spelling(
    ch: _CH, subject: str, spellings: list[str]
) -> None:
    assert await aliases.read_aliases(ORG, subject, ch) == spellings


@pytest.mark.asyncio
async def test_a_revocation_still_reaches_an_unverified_email(ch: _CH) -> None:
    # Reaching further fails closed: revoking the address ends the account it signs in to.
    assert await aliases.spellings(ORG, "bob@corp.example") == ["bob@corp.example", "mallory"]
    assert await aliases.spellings(ORG, "mallory") == ["mallory", "bob@corp.example"]


@pytest.mark.asyncio
async def test_a_grant_to_the_address_is_written_under_it_until_it_is_verified(ch: _CH) -> None:
    assert await aliases.canonical_subject(ORG, "bob@corp.example", ch=ch) == "bob@corp.example"
    assert await aliases.canonical_subject(ORG, "carol@corp.example", ch=ch) == "u-carol"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "ranks"),
    [
        # The address's grants cap a claim there (one in a token minted
        # before this read), never admit one.
        ("mallory", {"team-x": 2, "team-y": 0}),
        ("u-carol", {"team-x": 2, "team-y": 2}),
        ("dave", {"team-x": 2, "team-y": 2}),
    ],
)
async def test_the_gate_admits_a_grant_under_a_verified_email_only(
    ch: _CH, subject: str, ranks: dict[str, int]
) -> None:
    assert (await grant_cache._read(ORG, subject)).ranks == ranks


@pytest.mark.asyncio
async def test_grants_list_folds_a_verified_email_only(ch: _CH) -> None:
    people = await grants.people_page(ORG, "team-x", after=None, limit=10, ch=ch)
    # One row per person, its spellings in any order.
    assert {frozenset([p.subject, *p.also]) for p in people} == {
        frozenset(["bob@corp.example"]),
        frozenset(["mallory"]),
        frozenset(["u-carol", "carol@corp.example"]),
        frozenset(["dave", "dave@corp.example"]),
    }
