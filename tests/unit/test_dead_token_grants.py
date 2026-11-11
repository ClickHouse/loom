"""A token's own `sa:` subject grants nothing once its token is dead.

A token minted from the dashboard or by agent sign-up holds direct grants under its own
`sa:` subject, listed in `loom.service_accounts`. Once that row is revoked or past its
expiry, the token is refused at verify, but a trusted caller acting for the subject would
still reach its grants. The gate's grant reads, run here on clickhouse-local against the
production tables, count such a grant as rank 0: it still caps a claim, it never admits.
A subject no row lists (an operator's `loom mint-token --subject sa:<name>`) keeps its
grants until it is revoked outright.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core import grant_cache
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = [
    pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary"),
    pytest.mark.claim_cap,  # the grant cache reads real rows here
]

_TABLES = [ddl("namespace_grants"), ddl("namespaces"), ddl("sso_links"), ddl("service_accounts")]


def _grant(subject: str, namespace: str = "incidents", role: str = "writer") -> str:
    return (
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_by, version)"
        f" VALUES ('acme', '{namespace}', '{subject}', '{role}', 'alice', 1)"
    )


def _token(subject: str, *, expires: str = "NULL", revoked: str = "NULL") -> str:
    return (
        "INSERT INTO loom.service_accounts (org, name, sa_subject, namespaces, roles,"
        " created_by, expires_at, revoked_at) VALUES ('acme', 'triage', "
        f"'{subject}', ['incidents'], ['writer'], 'alice', {expires}, {revoked})"
    )


_LIVE = "now() + INTERVAL 1 DAY"
_PAST = "now() - INTERVAL 1 MINUTE"


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import server.core.ch as ch

    def use(*setup: str) -> LocalCH:
        local = LocalCH(*_TABLES, *setup)
        monkeypatch.setattr(ch, "get_client", lambda: local)
        return local

    return use


@pytest.mark.parametrize(
    ("row", "rank"),
    [
        ({"expires": _LIVE}, 2),
        ({}, 2),  # no expiry
        ({"expires": _PAST}, 0),
        ({"revoked": "now()"}, 0),
    ],
)
def test_the_grant_map_counts_a_dead_tokens_grant_as_rank_zero(
    client: Any, row: dict[str, str], rank: int
) -> None:
    client(_grant("sa:triage-a1b2c3"), _token("sa:triage-a1b2c3", **row))
    grants = asyncio.run(grant_cache._read("acme", "sa:triage-a1b2c3"))
    assert grants.rank("incidents") == rank  # the row is kept: it still caps the claim


def test_a_subject_no_token_row_lists_keeps_its_grants(client: Any) -> None:
    """`loom mint-token --subject sa:triage` writes no row: the operator ends it by subject."""
    client(_grant("sa:triage"), _token("sa:triage-a1b2c3", revoked="now()"))
    assert asyncio.run(grant_cache._read("acme", "sa:triage")).rank("incidents") == 2


def test_a_dead_token_in_another_org_ends_nothing_here(client: Any) -> None:
    client(
        _grant("sa:triage-a1b2c3"),
        _token("sa:triage-a1b2c3", revoked="now()").replace("'acme'", "'other'"),
    )
    assert asyncio.run(grant_cache._read("acme", "sa:triage-a1b2c3")).rank("incidents") == 2


@pytest.mark.parametrize(("row", "role"), [({"expires": _PAST}, None), ({}, "WRITER")])
def test_the_restricted_grant_read_refuses_a_dead_token(
    client: Any, row: dict[str, str], role: str | None
) -> None:
    from server.auth import authz

    client(_grant("sa:triage-a1b2c3"), _token("sa:triage-a1b2c3", **row))
    found = asyncio.run(authz.grant_role_for("acme", "incidents", "sa:triage-a1b2c3"))
    assert (found.name if found else None) == role


@pytest.mark.parametrize(("row", "inherits"), [({"expires": _PAST}, None), ({}, "alice")])
def test_an_expired_token_inherits_no_creator(row: dict[str, str], inherits: str | None) -> None:
    from server.auth import service_accounts

    service_accounts._creators.clear()
    local = LocalCH(*_TABLES, _token("sa:triage-a1b2c3", **row))
    found = asyncio.run(service_accounts.inheriting_creator("acme", "sa:triage-a1b2c3", ch=local))
    assert found == inherits


def _kept(path: Any, monkeypatch: pytest.MonkeyPatch, *setup: str) -> Any:
    import server.core.ch as ch
    from tests.unit.clickhouse_local import KeptCH

    kept = KeptCH(path, *_TABLES, ddl("acl_versions"))
    for statement in setup:
        asyncio.run(kept.execute(statement))
    monkeypatch.setattr(ch, "get_client", lambda: kept)
    return kept


def test_a_name_taken_again_revokes_its_former_tokens_grants(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new row replaces the former one under FINAL, so no row would list the former
    subject as dead: its grants are revoked before the name is taken."""
    from server.auth.delegation import Delegated
    from server.core.datetimes import naive_utc_now_seconds
    from server.identity.app import me_service_accounts

    kept = _kept(
        tmp_path,
        monkeypatch,
        _grant("sa:triage-a1b2c3"),
        _token("sa:triage-a1b2c3", expires=_PAST),
    )
    delegated = Delegated(
        org="acme",
        namespaces=["team"],
        roles=["viewer"],
        ns_roles=None,
        write_namespaces=None,
        read_scope="",
        read_user_id="",
        ttl_seconds=None,
        parents=[],
        parent_subjects=[],
        parent_iats=[],
        creator="bob",
        is_service_account=True,
    )
    asyncio.run(
        me_service_accounts.save_token(
            "triage", "sa:triage-d4e5f6", delegated, created_by="bob", now=naive_utc_now_seconds()
        )
    )
    held = asyncio.run(
        kept.query_all("SELECT sa_subject FROM loom.service_accounts FINAL WHERE name = 'triage'")
    )
    assert held == [{"sa_subject": "sa:triage-d4e5f6"}]
    rows = asyncio.run(
        kept.query_all(
            "SELECT subject, revoked_at IS NOT NULL AS revoked FROM loom.namespace_grants FINAL"
        )
    )
    assert rows == [{"subject": "sa:triage-a1b2c3", "revoked": 1}]
    grant_cache.clear()
    assert asyncio.run(grant_cache._read("acme", "sa:triage-a1b2c3")).rank("incidents") == 0


def test_a_token_ended_with_its_creator_drops_its_cached_grants(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`revoke_created_by` marks the row revoked and writes no grant row: the grant
    cache and the creator memo must drop the token's entry at once, here, and move
    the watermark for every other process."""
    from server.auth import revocation, service_accounts

    kept = _kept(
        tmp_path,
        monkeypatch,
        _grant("sa:triage-a1b2c3"),
        _token("sa:triage-a1b2c3").replace("'alice'", "'lee'"),
    )
    monkeypatch.setattr(revocation, "_loaded", True)

    async def outright(subjects: list[str], *, permanent: bool) -> dict[str, int | None]:
        return dict.fromkeys(subjects)  # each one revoked outright

    monkeypatch.setattr(revocation, "cutoffs", outright)

    async def run() -> int:
        assert (await grant_cache.grant_map("acme", "sa:triage-a1b2c3")).rank("incidents") == 2
        assert await service_accounts.inheriting_creator("acme", "sa:triage-a1b2c3") == "lee"
        dead = await service_accounts.revoke_created_by(["lee"], revoked_by="scim")
        assert dead == {"sa:triage-a1b2c3"}
        assert await service_accounts.inheriting_creator("acme", "sa:triage-a1b2c3") is None
        return (await grant_cache.grant_map("acme", "sa:triage-a1b2c3")).rank("incidents")

    service_accounts._creators.clear()
    assert asyncio.run(run()) == 0
    marks = asyncio.run(kept.query_all("SELECT org, kind FROM loom.acl_versions"))
    assert marks == [{"org": "acme", "kind": "grants"}]
