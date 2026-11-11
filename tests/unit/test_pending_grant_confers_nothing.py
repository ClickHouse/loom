"""A pending join request grants nothing — in the gate and in the lists.

`namespace.join` on an approval-gated namespace writes a `status='pending'`
grant row. The per-namespace gate ignores it, and so must the namespace lists
the search fan-out and the org-wide rollups trust (`resolve_my_namespaces`), or
a requester reads the namespace before any admin approves. The SQL
each path really issues runs here on `clickhouse local` against the
production table definition.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from server.auth import aliases, authz, context
from server.auth.jwt import AuthInfo
from server.operations.base import Role
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

# The fixture below stubs the alias lookup; one test reads the real one.
_REAL_ALIASES = aliases.subject_aliases
# `claim_cap`: the gate reads the real grant rows through the grant cache.
pytestmark = [
    pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed"),
    pytest.mark.claim_cap,
]

_ROWS = """INSERT INTO loom.namespace_grants
(org, namespace, subject, role, granted_by, revoked_at, version, status) VALUES
('o', 'mine',   'eve',    'admin',  'eve', NULL, 1, 'active'),
('o', 'gated',  'eve',    'viewer', 'eve', NULL, 1, 'pending'),
('o', 'gone',   'eve',    'writer', 'op',  NULL, 1, 'active'),
('o', 'gone',   'eve',    'writer', 'op',  '2026-01-01 00:00:00', 2, 'active'),
('o', 'theirs', 'carol',  'writer', 'op',  NULL, 1, 'active'),
('o', 'asked',  'carol',  'writer', 'carol', NULL, 1, 'pending'),
('o', 'asked',  'mallory', 'admin', 'mallory', NULL, 1, 'pending'),
('o', 'team',   'Dave',   'writer', 'op',  NULL, 1, 'active'),
('o', 'mailed', 'f@x.com', 'writer', 'op', NULL, 1, 'active');
INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email) VALUES
('o', 'home-frank', 'frank', 'f@x.com')"""


class _LocalCH:
    """Runs each query on clickhouse-local over the fixture rows."""

    def _run(self, sql: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        return run(
            # Each namespace's classification and the accounts that tie a
            # person's spellings.
            [
                ddl("namespace_grants"),
                ddl("namespaces"),
                ddl("sso_links"),
                ddl("service_accounts"),
                _ROWS,
            ],
            sql,
            params,
        )

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._run(sql, params)

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        rows = self._run(sql, params)
        return rows[0] if rows else None


@pytest.fixture(autouse=True)
def _local(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    ch = _LocalCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def aliases(org: str, subject: str, **_: Any) -> list[str]:
        return [subject]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", aliases)
    yield
    context.set_current_auth(None)


def _as(subject: str) -> None:
    context.set_current_auth(
        AuthInfo(
            subject=subject, org="o", namespaces=(), roles=(Role.VIEWER,), is_service_account=False
        )
    )


def test_the_namespace_list_skips_pending_and_revoked_grants() -> None:
    _as("eve")
    assert asyncio.run(context.resolve_my_namespaces("o")) == ["mine"]


def test_the_gate_and_the_list_agree() -> None:
    _as("eve")
    listed = set(asyncio.run(context.resolve_my_namespaces("o")))
    granted = {
        ns
        for ns in ("mine", "gated", "gone", "theirs")
        if asyncio.run(authz.grant_role_for("o", ns, "eve")) is not None
    }
    assert listed == granted


def test_a_grant_under_another_case_is_listed_and_admitted() -> None:
    _as("dave")
    assert asyncio.run(context.resolve_my_namespaces("o")) == ["team"]
    assert asyncio.run(authz.grant_role_for("o", "team", "dave")) is not None


def test_an_alias_owner_in_another_case_reaches_the_grant_under_its_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliases._CACHE.clear()
    monkeypatch.setattr("server.auth.aliases.subject_aliases", _REAL_ALIASES)
    _as("Frank")
    assert asyncio.run(context.resolve_my_namespaces("o")) == ["mailed"]
    assert asyncio.run(authz.grant_role_for("o", "mailed", "Frank")) is not None


def test_a_service_account_inherits_only_its_creators_live_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _LocalCH()

    class _WithSA(_LocalCH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            if (
                "loom.service_accounts" in sql and "namespace_grants" not in sql
            ):  # not the grant read's dead-token set
                return [{"created_by": "carol", "inherit_creator_grants": True}]
            return await real.query_all(sql, params)

        async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            rows = await self.query_all(sql, params)
            return rows[0] if rows else None

    ch = _WithSA()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    _as("sa:bot-1")
    assert asyncio.run(context.resolve_my_namespaces("o")) == ["theirs"]


def test_the_me_mint_paths_read_no_pending_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Setup and service-account mints cap each namespace at the caller's
    grant; a pending admin request is not one."""
    import server.identity.app.me_callers as me

    async def aliases(org: str, subject: str) -> list[str]:
        return [subject]

    monkeypatch.setattr(me, "subject_aliases_of", aliases)
    assert asyncio.run(me.caller_role_on("o", "asked", "mallory")) is None
    assert asyncio.run(me.caller_admin_of("o", "asked", "mallory")) is False
    assert asyncio.run(me.caller_role_on("o", "mine", "eve")) == "admin"
    assert asyncio.run(me.caller_admin_of("o", "mine", "eve")) is True


def test_a_pending_admin_request_does_not_count_as_an_admin() -> None:
    from server.identity.store import grants as grants_repo

    assert asyncio.run(grants_repo.count_active_admins("o", "asked")) == 0
    assert asyncio.run(grants_repo.count_active_admins("o", "mine")) == 1
