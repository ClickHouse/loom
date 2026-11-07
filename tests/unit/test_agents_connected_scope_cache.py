"""agents.connected caches one answer per caller scope, never per param list alone.

An org admin's filter hides the restricted namespaces it holds no grant for
(NOT IN); a member's filter keeps the namespaces it may read (IN). With equal
lists the two are opposite scopes, so they bind different param names and the
60 s cache keys on the whole predicate: the second caller never gets the
first caller's rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

import server.insights.app.agents as agents_mod
from server.auth import context
from server.auth.jwt import AuthInfo
from server.insights.api.models_agents import AgentsConnectedInput
from server.operations.base import Role

ORG = "acme"
RESTRICTED = "restricted-ns"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def hidden(org: str) -> list[str]:
        return [RESTRICTED]

    async def mine(org: str, subject: str | None = None) -> list[str]:
        return [RESTRICTED]

    monkeypatch.setattr(context, "restricted_namespaces_hidden_from", hidden)
    monkeypatch.setattr(context, "resolve_my_namespaces", mine)
    agents_mod.reset_connected_cache()
    yield
    agents_mod.reset_connected_cache()
    context.set_current_auth(None)


def _as(role: Role) -> None:
    # An admin reads the whole org through a `*` claim; a listed claim reads as a member.
    claim = ("*",) if role.value >= Role.ORG_ADMIN.value else ()
    context.set_current_auth(
        AuthInfo(subject="who", org=ORG, namespaces=claim, roles=(role,), is_service_account=False)
    )


def _scope(role: Role) -> context.OrgScopeFilter:
    _as(role)
    return asyncio.run(context.resolve_org_scope(ORG))


def test_exclusion_and_inclusion_with_equal_lists_bind_different_params() -> None:
    admin, member = _scope(Role.ORG_ADMIN), _scope(Role.VIEWER)
    assert admin.params["ns_hidden"] == [RESTRICTED]
    assert member.params == {"ns_list": [RESTRICTED]}
    assert "NOT IN {ns_hidden:Array(String)}" in admin.where
    assert " IN {ns_list:Array(String)}" in member.where and "NOT IN" not in member.where


def test_admin_and_member_with_equal_lists_get_separate_cache_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeCH:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            calls.append((sql, dict(params or {})))
            ns = "every-other-ns" if "namespace NOT IN" in sql else RESTRICTED
            return [
                {
                    "agent": f"agent-in-{ns}",
                    "last_seen": "2026-01-01 00:00:00",
                    "first_seen": "2026-01-01 00:00:00",
                    "ops": 1,
                    "namespaces_touched": 1,
                    "primary_namespace": ns,
                    "cost_usd": 0.0,
                }
            ]

    monkeypatch.setattr(agents_mod, "get_client", lambda: _FakeCH())

    async def ask(role: Role) -> list[str]:
        _as(role)
        out = await agents_mod.handle_agents_connected(AgentsConnectedInput(org=ORG))
        return [r.primary_namespace for r in out.rows]

    async def run() -> tuple[list[str], list[str], list[str]]:
        return await ask(Role.ORG_ADMIN), await ask(Role.VIEWER), await ask(Role.ORG_ADMIN)

    admin, member, admin_again = asyncio.run(run())
    assert admin == ["every-other-ns"]
    assert member == [RESTRICTED], "the member did not get the admin's cached rows"
    assert admin_again == admin
    assert len(calls) == 2, "each scope reads once, then serves its own cache entry"
    assert len(agents_mod._connected_cache) == 2
