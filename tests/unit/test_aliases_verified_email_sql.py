"""An account's email is another spelling of its owner only once it is verified, on
clickhouse-local against the real tables: an SSO account's (no password; the identity
provider vouched for it), or a password account linked to it in `loom.sso_links`. A
self-signup email is unverified, so whoever typed someone else's address first holds
none of the grants written under it: not at the gate, not in the alias reads, not in
`grants.list`'s fold."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from server.auth import aliases, sso_links
from server.core import grant_cache
from server.identity.store import grants
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, LocalCH, ddl

pytestmark = [
    pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed"),
    pytest.mark.claim_cap,  # the grant map reads real rows here
]

ORG = "acme"


def _account(namespace: str, email: str, password_hash: str, active: int = 1) -> str:
    return (
        "INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email,"
        " password_hash, active, created_at, updated_at) VALUES"
        f" ('{ORG}', '{namespace}', '{namespace}', '{email}', '{password_hash}', {active},"
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
    # Their personal namespaces: an owner's second row, naming no email.
    "INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email, password_hash,"
    f" active, created_at, updated_at) VALUES ('{ORG}', 'personal-u-carol', 'u-carol', '', '',"
    f" 1, now(), now()), ('{ORG}', 'personal-dave', 'dave', '', '', 1, now(), now())",
    # A password account linked to its email.
    _account("dave", "dave@corp.example", "argon2-hash"),
    "INSERT INTO loom.sso_links (email, org, namespace, linked, linked_by, linked_at, version)"
    f" VALUES ('dave@corp.example', '{ORG}', 'dave', 1, 'op', now(), 1)",
    # A password account linked to its email, then unlinked: unverified again.
    _account("erin", "erin@corp.example", "argon2-hash"),
    "INSERT INTO loom.sso_links (email, org, namespace, linked, linked_by, linked_at, version)"
    f" VALUES ('erin@corp.example', '{ORG}', 'erin', 1, 'op', now(), 1),"
    f" ('erin@corp.example', '{ORG}', 'erin', 0, 'op', now(), 2)",
    # A deactivated SSO account.
    _account("u-frank", "frank@corp.example", "", active=0),
    # Each address granted before its person signed in, and each handle on team-x.
    *(
        _grant(ns, f"{who}@corp.example")
        for ns in ("team-x", "team-y")
        for who in ("bob", "carol", "dave")
    ),
    *(_grant("team-y", f"{who}@corp.example") for who in ("erin", "frank")),
    *(_grant("team-x", handle) for handle in ("mallory", "u-carol", "dave")),
    _grant("team-z", "mallory"),
    # Granted under the address as typed, before the account was linked.
    _grant("team-w", "Dave@Corp.example"),
    # Carol's address, verified as u-carol's, typed by a self-signup account too,
    # which holds a grant of its own on team-u, where Carol holds none.
    _account("mallory3", "carol@corp.example", "argon2-hash"),
    _grant("team-u", "mallory3"),
    # On team-v the address's own row was revoked, and mallory holds one of her own.
    "INSERT INTO loom.namespace_grants (org, namespace, subject, role, status, granted_at,"
    f" granted_by, revoked_at, version) VALUES ('{ORG}', 'team-v', 'bob@corp.example',"
    " 'writer', 'active', now(), 'admin', now(), 1)",
    _grant("team-v", "mallory"),
)


# An SSO account whose login handle is its address.
_GINA = (
    "INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email,"
    " password_hash, active, created_at, updated_at) VALUES"
    f" ('{ORG}', 'gina', 'gina@corp.example', 'gina@corp.example', '', 1, now(), now())"
)


class _CH(LocalCH):
    async def query_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


@pytest.fixture(autouse=True)
def _no_cached_aliases() -> Iterator[None]:
    aliases._CACHE.clear()
    yield
    aliases._CACHE.clear()


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
        ("erin", ["erin"]),
        ("u-frank", ["u-frank"]),  # deactivated: no spelling admits
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
    assert await aliases.canonical_subject(ORG, "dave@corp.example", ch=ch) == "dave"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "ranks"),
    [
        # The address's grants cap a claim there (one in a token minted
        # before this read), never admit one.
        ("mallory", {"team-x": 2, "team-y": 0, "team-z": 2, "team-v": 2}),
        # Nor does the squatter's own grant admit the address.
        ("bob@corp.example", {"team-x": 2, "team-y": 2, "team-z": 0, "team-v": 0}),
        ("u-carol", {"team-x": 2, "team-y": 2}),
        ("dave", {"team-x": 2, "team-y": 2, "team-w": 2}),  # team-w: in any case
        ("erin", {"team-y": 0}),  # unlinked
        ("u-frank", {"team-y": 0}),  # deactivated
    ],
)
async def test_the_gate_admits_a_grant_under_a_verified_email_only(
    ch: _CH, subject: str, ranks: dict[str, int]
) -> None:
    assert (await grant_cache._read(ORG, subject)).ranks == ranks


@pytest.mark.asyncio
async def test_a_link_or_an_unlink_drops_the_cached_ties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = KeptCH(tmp_path, *SETUP, ddl("acl_versions"))
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(sso_links, "get_client", lambda: kept)
    assert await aliases.subject_aliases(ORG, "dave", ch=kept) == ["dave", "dave@corp.example"]
    assert await aliases.subject_aliases(ORG, "erin", ch=kept) == ["erin"]

    await sso_links.unlink("dave@corp.example", [(ORG, "dave")], "op")
    await sso_links.link("erin@corp.example", [(ORG, "erin")], "op")

    # Read again, not from the cache: the unlink unties, the link ties.
    assert await aliases.subject_aliases(ORG, "dave", ch=kept) == ["dave"]
    assert await aliases.subject_aliases(ORG, "erin", ch=kept) == ["erin", "erin@corp.example"]
    # And every other process drops the org's grant maps at its next poll.
    marks = await kept.query_all(
        "SELECT sum(changes) AS n FROM loom.acl_versions WHERE org = {org:String}",
        {"org": ORG},
    )
    assert marks[0]["n"] == 2


class _LostReply(KeptCH):
    """Writes a link row, then loses the reply."""

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        await super().insert(table, rows, settings)
        if table == "loom.sso_links":
            raise TimeoutError("reply lost")


@pytest.mark.asyncio
async def test_an_unlink_whose_reply_is_lost_still_drops_the_cached_ties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = _LostReply(tmp_path, *SETUP, ddl("acl_versions"))
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(sso_links, "get_client", lambda: kept)
    assert await aliases.subject_aliases(ORG, "dave", ch=kept) == ["dave", "dave@corp.example"]

    with pytest.raises(TimeoutError):
        await sso_links.unlink("dave@corp.example", [(ORG, "dave")], "op")

    assert (ORG, "dave") not in aliases._CACHE
    assert await aliases.subject_aliases(ORG, "dave", ch=kept) == ["dave"]
    marks = await kept.query_all(
        "SELECT sum(changes) AS n FROM loom.acl_versions WHERE org = {org:String}",
        {"org": ORG},
    )
    assert marks[0]["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "held"),
    [
        ("bob@corp.example", False),  # named by mallory only, unverified
        ("carol@corp.example", True),  # u-carol's verified email
        ("Gina@Corp.example", True),  # an SSO account whose handle is the address
        ("frank@corp.example", False),  # its SSO account is deactivated
    ],
)
async def test_an_address_is_held_by_a_verified_account_or_a_handle_that_is_it(
    monkeypatch: pytest.MonkeyPatch, address: str, held: bool
) -> None:
    client = _CH(*SETUP, _GINA)
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    assert (await aliases.verified_tie(ORG, address))[1] is held


@pytest.mark.asyncio
async def test_revoking_an_address_whose_handle_is_it_leaves_a_squatters_row_and_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gina's handle is her address; mallory4 typed it too and holds a row on
    # team-g under her own handle. Revoking the address is revoking gina, who holds
    # none there: mallory4's row and her token's grant stay.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import grants as grants_app
    from server.identity.store import grants as grants_repo

    kept = KeptCH(
        tmp_path,
        *SETUP,
        _GINA,
        _account("mallory4", "gina@corp.example", "argon2-hash"),
        _grant("team-g", "mallory4"),
        ddl("service_accounts"),
        ddl("acl_versions"),
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by) VALUES"
        f" ('{ORG}', 'bot', 'sa:m4-bot', 'mallory4')",
        _grant("team-g", "sa:m4-bot"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    body = GrantsRevokeInput(org=ORG, namespace="team-g", subject="gina@corp.example")
    assert (await grants_app.handle_grants_revoke(body)).revoked is False
    live = await kept.query_all(
        "SELECT subject FROM loom.namespace_grants FINAL WHERE org = {org:String}"
        " AND namespace = 'team-g' AND revoked_at IS NULL ORDER BY subject",
        {"org": ORG},
    )
    assert [r["subject"] for r in live] == ["mallory4", "sa:m4-bot"]


@pytest.mark.asyncio
async def test_revoking_the_address_reaches_the_tokens_its_squatter_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A token mallory made while the address's grant still admitted her holds a
    # grant of its own there; revoking the address's grant takes it too.
    from server.identity.app import token_grants
    from server.identity.store import grants as grants_repo

    kept = KeptCH(
        tmp_path,
        *SETUP,
        ddl("service_accounts"),
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by) VALUES"
        f" ('{ORG}', 'bot', 'sa:bot', 'mallory')",
        _grant("team-y", "sa:bot"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    touched = await token_grants.cascade_revoke_to_created_tokens(
        ORG, "team-y", "bob@corp.example", unheld=True, ch=kept
    )
    assert touched == 1
    live = await kept.query_all(
        "SELECT subject FROM loom.namespace_grants FINAL WHERE subject = 'sa:bot'"
        " AND revoked_at IS NULL"
    )
    assert live == []


@pytest.mark.asyncio
async def test_revoking_a_grant_reaches_the_tokens_made_under_another_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.app import token_grants
    from server.identity.store import grants as grants_repo

    kept = KeptCH(
        tmp_path,
        *SETUP,
        ddl("service_accounts"),
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by) VALUES"
        f" ('{ORG}', 'bot', 'sa:bot', 'Mallory')",
        _grant("team-y", "sa:bot"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    touched = await token_grants.cascade_revoke_to_created_tokens(
        ORG, "team-y", "mallory", unheld=False, ch=kept
    )
    assert touched == 1


@pytest.mark.asyncio
async def test_revoking_a_handle_leaves_the_tokens_its_unverified_email_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A token whose creator is the address may be the real owner's: revoking the
    # squatter's handle does not reach it.
    from server.identity.app import token_grants
    from server.identity.store import grants as grants_repo

    kept = KeptCH(
        tmp_path,
        *SETUP,
        ddl("service_accounts"),
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by) VALUES"
        f" ('{ORG}', 'bot', 'sa:bob-bot', 'bob@corp.example')",
        _grant("team-x", "sa:bob-bot"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    assert (
        await token_grants.cascade_revoke_to_created_tokens(
            ORG, "team-x", "mallory", unheld=False, ch=kept
        )
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("named", ["u-carol", "carol@corp.example"])
async def test_revoking_a_verified_person_leaves_a_squatters_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: str
) -> None:
    # u-carol's row on team-y is stored under her verified email, which a
    # self-signup account also typed. Revoking her, by handle or by address,
    # takes that row; the tokens the other account made stay, since a verified
    # account holds the address.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import grants as grants_app
    from server.identity.store import grants as grants_repo

    kept = KeptCH(
        tmp_path,
        *SETUP,
        ddl("service_accounts"),
        ddl("acl_versions"),
        ddl("audit_log"),
        _account("mallory2", "carol@corp.example", "argon2-hash"),
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by) VALUES"
        f" ('{ORG}', 'bot', 'sa:m2-bot', 'mallory2')",
        _grant("team-y", "sa:m2-bot"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr("server.observability.audit.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    body = GrantsRevokeInput(org=ORG, namespace="team-y", subject=named)
    assert (await grants_app.handle_grants_revoke(body)).revoked is True
    live = await kept.query_all(
        "SELECT subject FROM loom.namespace_grants FINAL WHERE org = {org:String}"
        " AND namespace = 'team-y' AND revoked_at IS NULL",
        {"org": ORG},
    )
    subjects = {r["subject"] for r in live}
    assert "sa:m2-bot" in subjects and "carol@corp.example" not in subjects


@pytest.mark.asyncio
@pytest.mark.parametrize("own_status", ["active", "pending"])
async def test_revoking_an_address_leaves_the_tokens_of_an_account_with_its_own_grant(
    own_status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On team-x mallory holds a grant under her handle as well as one under the
    # address she typed. Revoking the address takes its row; her tokens there may be
    # what her own grant gave, so they keep their grants. A pending request of her
    # own admits nothing, so it keeps nothing.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import grants as grants_app
    from server.identity.store import grants as grants_repo

    kept = KeptCH(
        tmp_path,
        *SETUP,
        ddl("service_accounts"),
        ddl("acl_versions"),
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by) VALUES"
        f" ('{ORG}', 'bot', 'sa:m-bot', 'mallory')",
        _grant("team-x", "sa:m-bot"),
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, status,"
        " granted_at, granted_by, version) VALUES"
        f" ('{ORG}', 'team-x', 'mallory', 'viewer', '{own_status}', now(), 'admin', 2)",
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    body = GrantsRevokeInput(org=ORG, namespace="team-x", subject="bob@corp.example")
    assert (await grants_app.handle_grants_revoke(body)).revoked is True
    live = await kept.query_all(
        "SELECT subject FROM loom.namespace_grants FINAL WHERE org = {org:String}"
        " AND namespace = 'team-x' AND revoked_at IS NULL",
        {"org": ORG},
    )
    subjects = {r["subject"] for r in live}
    assert "mallory" in subjects and "bob@corp.example" not in subjects
    assert ("sa:m-bot" in subjects) is (own_status == "active")


@pytest.mark.asyncio
async def test_revoking_an_address_takes_its_squatters_row_and_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The address never held a row on team-z; the one stored for it under mallory's
    # handle goes, and so do the grants of the tokens mallory made there.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import grants as grants_app
    from server.identity.store import grants as grants_repo

    kept = KeptCH(
        tmp_path,
        *SETUP,
        ddl("service_accounts"),
        ddl("acl_versions"),
        "INSERT INTO loom.service_accounts (org, name, sa_subject, created_by) VALUES"
        f" ('{ORG}', 'bot', 'sa:m-bot', 'mallory')",
        _grant("team-z", "sa:m-bot"),
        ddl("audit_log"),
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr("server.observability.audit.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    body = GrantsRevokeInput(org=ORG, namespace="team-z", subject="bob@corp.example")
    assert (await grants_app.handle_grants_revoke(body)).revoked is True
    live = await kept.query_all(
        "SELECT subject FROM loom.namespace_grants FINAL WHERE org = {org:String}"
        " AND namespace = 'team-z' AND revoked_at IS NULL",
        {"org": ORG},
    )
    assert live == []
    # The audit log names whose row went, not only the address asked for.
    audited = await kept.query_all(
        "SELECT JSONExtractString(subject, 'subject') AS s FROM loom.audit_log"
        " WHERE event_type = 'grants.revoke'"
    )
    assert "mallory" in {r["s"] for r in audited}


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


@pytest.mark.asyncio
async def test_the_user_list_marks_a_platform_admin_by_a_verified_email_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_admin_users import AdminUsersInput
    from server.identity.app import admin_users
    from server.operations.base import Role
    from server.settings import settings

    client = _CH(*SETUP, ddl("audit_log"))
    monkeypatch.setattr(admin_users, "get_client", lambda: client)
    listed = {f"{who}@corp.example" for who in ("bob", "carol", "dave", "erin")}
    monkeypatch.setattr(type(settings), "platform_admins_set", property(lambda _s: listed))
    set_current_auth(
        AuthInfo(
            subject="op",
            org=ORG,
            namespaces=("*",),
            roles=(Role.ORG_ADMIN,),
            is_service_account=False,
        )
    )
    try:
        out = await admin_users.handle_admin_users(AdminUsersInput(org=ORG))
    finally:
        set_current_auth(None)
    marked = {u.subject: u.is_platform_admin for u in out.users}
    assert {s: marked[s] for s in ("mallory", "u-carol", "dave", "erin")} == {
        "mallory": False,
        "u-carol": True,
        "dave": True,
        "erin": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("namespace", "subject", "found"),
    [
        # Every row the gate admits the person by: the handle's and its verified
        # email's, whichever spelling is revoked.
        ("team-x", "dave@corp.example", ["dave", "dave@corp.example"]),
        ("team-x", "dave", ["dave", "dave@corp.example"]),
        ("team-y", "u-carol", ["carol@corp.example"]),
        # An address with a row of its own takes that row only.
        ("team-x", "bob@corp.example", ["bob@corp.example"]),
        # With none, it reaches its squatter's handle's row.
        ("team-z", "bob@corp.example", ["mallory"]),
        # A handle never reaches its unverified email's row.
        ("team-y", "mallory", []),
        # An address whose own row was revoked there takes nothing more: revoke the
        # handle's row by the handle.
        ("team-v", "bob@corp.example", []),
        # An address typed in another case still reaches the row kept in lower case.
        ("team-x", "Carol@Corp.example", ["carol@corp.example", "u-carol"]),
        # And a row stored in another case is reached by the handle or the address.
        ("team-w", "dave", ["Dave@Corp.example"]),
        ("team-w", "dave@corp.example", ["Dave@Corp.example"]),
        # An address a verified account holds is that person: no fallback to the
        # row of an unverified account that typed it too.
        ("team-u", "carol@corp.example", []),
    ],
)
async def test_a_revoke_takes_the_rows_its_spellings_reach(
    ch: _CH, monkeypatch: pytest.MonkeyPatch, namespace: str, subject: str, found: list[str]
) -> None:
    from server.identity.app import grants as grants_app

    monkeypatch.setattr(grants, "_default_client", lambda: ch)
    rows, _ = await grants_app._live_grant_rows(ORG, namespace, subject)
    assert sorted(stored for stored, _ in rows) == found


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "roles"),
    [
        # Its personal namespace's row names no email; the account's verified one counts.
        ("u-carol", ["platform_admin"]),
        # An unverified email keeps no role, whoever's address it is.
        ("mallory", ["writer"]),
        ("nobody", None),  # no account left: the refresh chain ends
    ],
)
async def test_a_refresh_keeps_platform_admin_by_a_verified_email_only(
    ch: _CH, monkeypatch: pytest.MonkeyPatch, subject: str, roles: list[str] | None
) -> None:
    from server.auth import login
    from server.settings import settings

    monkeypatch.setattr(login, "get_client", lambda: ch)
    listed = {"carol@corp.example", "bob@corp.example"}
    monkeypatch.setattr(type(settings), "platform_admins_set", property(lambda _s: listed))
    assert await login.refreshed_roles(subject, ORG, ["platform_admin"]) == roles


@pytest.mark.asyncio
async def test_revoking_an_address_again_takes_nothing_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The address's own row goes; a retry finds it revoked and does not fall back
    # to the row of the unverified account naming it, which is that account's own.
    from server.identity.api.models_grants import GrantsRevokeInput
    from server.identity.app import grants as grants_app
    from server.identity.app import token_grants
    from server.identity.store import grants as grants_repo

    kept = KeptCH(tmp_path, *SETUP, ddl("service_accounts"), ddl("acl_versions"))
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr(grants_repo, "_default_client", lambda: kept)
    cascade, cascaded = token_grants.cascade_revoke_to_created_tokens, []

    async def _counted(*a: Any, **k: Any) -> int:
        cascaded.append(k["taken"])
        return await cascade(*a, **k)

    monkeypatch.setattr(token_grants, "cascade_revoke_to_created_tokens", _counted)
    body = GrantsRevokeInput(org=ORG, namespace="team-x", subject="bob@corp.example")
    assert (await grants_app.handle_grants_revoke(body)).revoked is True
    assert (await grants_app.handle_grants_revoke(body)).revoked is False
    assert cascaded == [("bob@corp.example",)]  # none for the retry that took nothing
    live = await kept.query_all(
        "SELECT subject FROM loom.namespace_grants FINAL WHERE org = {org:String}"
        " AND namespace = 'team-x' AND revoked_at IS NULL",
        {"org": ORG},
    )
    subjects = {r["subject"] for r in live}
    assert "mallory" in subjects and "bob@corp.example" not in subjects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("namespace", "email", "held"),
    [
        # Carol's address is u-carol's verified email: held elsewhere.
        ("mallory3", "carol@corp.example", True),
        # Bob's is named only by mallory, unverified.
        ("mallory", "bob@corp.example", False),
        # u-frank is deactivated: his address is held by no active account.
        ("someone", "frank@corp.example", False),
    ],
)
async def test_an_old_address_is_held_elsewhere_only_by_a_verified_account(
    ch: _CH, namespace: str, email: str, held: bool
) -> None:
    from server.identity.app import account_admin

    assert await account_admin._verified_elsewhere(ORG, namespace, email) is held


@pytest.mark.asyncio
async def test_a_link_away_from_a_verified_email_leaves_its_grants_and_caps_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # mallory3 typed Carol's address, which u-carol holds verified. Linking mallory3
    # to an address of her own leaves Carol's grants under it live, and carries each
    # to mallory3 revoked, so a token minted under the old tie stays capped.
    from contextlib import asynccontextmanager

    from server.core import leases
    from server.identity.app import account_admin

    @asynccontextmanager
    async def _held(*_a: Any, **_k: Any) -> Any:
        yield True

    kept = KeptCH(tmp_path, *SETUP, ddl("audit_log"), ddl("acl_versions"))
    monkeypatch.setattr("server.core.ch.get_client", lambda: kept)
    monkeypatch.setattr("server.observability.audit.get_client", lambda: kept)
    monkeypatch.setattr(sso_links, "get_client", lambda: kept)
    monkeypatch.setattr(grants, "_default_client", lambda: kept)
    monkeypatch.setattr(leases, "holding", _held)
    monkeypatch.setattr(account_admin.settings, "oauth_login_provider", "oidc")
    linked = await account_admin.link_account("mallory3@corp.example", ORG, "mallory3", by="op")
    assert linked == ("carol@corp.example", ["team-x", "team-y"], True)
    rows = await kept.query_all(
        "SELECT namespace, subject, revoked_at IS NULL AS live FROM loom.namespace_grants"
        " FINAL WHERE org = {org:String} AND subject IN ('mallory3', 'carol@corp.example')"
        " ORDER BY namespace, subject",
        {"org": ORG},
    )
    assert [(r["namespace"], r["subject"], int(r["live"])) for r in rows] == [
        ("team-u", "mallory3", 1),
        ("team-x", "carol@corp.example", 1),
        ("team-x", "mallory3", 0),
        ("team-y", "carol@corp.example", 1),
        ("team-y", "mallory3", 0),
    ]
    audited = await kept.query_all(
        "SELECT JSONExtractBool(subject, 'elsewhere') AS elsewhere FROM loom.audit_log"
        " WHERE event_type = 'auth.sso_linked'"
    )
    assert [int(r["elsewhere"]) for r in audited] == [1]
