"""The namespace resolvers drop every restricted namespace the
caller cannot read — for scoped and flat tokens alike — and an org admin's
rollup filter excludes them by name."""

from __future__ import annotations

import asyncio

import pytest

import server.auth.authz as authz
import server.core.grant_cache as grant_cache
import server.identity.app.namespace_classification as nc
from server.auth.context import (
    resolve_my_namespaces,
    resolve_org_scope,
    set_current_auth,
)
from server.auth.directory import ClassificationUnavailable
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "clickhouse"
PRIV = "code-example-private"

# The gate reads the subject's grants from the grant maps faked below.
pytestmark = pytest.mark.claim_cap


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    state = {"restricted": frozenset({PRIV}), "grants": {}, "grant_rows": []}

    async def _restricted_set(org):
        return state["restricted"] if org == ORG else frozenset()

    monkeypatch.setattr(nc, "restricted_set", _restricted_set)
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: None)
    monkeypatch.setattr(repos, "configured_repos", list)

    async def _grant(org, ns, subject):
        return state["grants"].get((org, ns, subject))

    monkeypatch.setattr(authz, "grant_role_for", _grant)

    async def _read(org, subject):
        from server.operations.base import grant_rank

        ranks = {
            ns: grant_rank(role.name.lower())
            for (o, ns, s), role in state["grants"].items()
            if o == org and s == subject
        }
        return grant_cache.GrantMap(ranks=ranks)

    monkeypatch.setattr(grant_cache, "_read", _read)

    class _Client:
        async def query_all(self, sql, params=None):
            if "namespace_grants" in sql:
                return [{"namespace": ns} for ns in state["grant_rows"]]
            return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr("server.settings.settings.restricted_serve", True, raising=False)
    yield state
    set_current_auth(None)


def _scoped(*names):
    return AuthInfo(
        subject="sa:clickgap",
        org=ORG,
        namespaces=tuple(names),
        roles=(),
        is_service_account=True,
        ns_roles=tuple((n, "viewer") for n in names),
    )


def _flat(names, roles=(Role.VIEWER,), subject="alice@x"):
    return AuthInfo(
        subject=subject,
        org=ORG,
        namespaces=tuple(names),
        roles=tuple(roles),
        is_service_account=False,
    )


def test_scoped_claim_keeps_order_and_drops_ungranted_restricted(world) -> None:
    set_current_auth(_scoped("clickgap-public", PRIV, "code-clickhouse"))
    assert asyncio.run(resolve_my_namespaces(ORG)) == ["clickgap-public", "code-clickhouse"]
    world["grants"][(ORG, PRIV, "sa:clickgap")] = Role.VIEWER
    grant_cache.clear()
    assert asyncio.run(resolve_my_namespaces(ORG)) == ["clickgap-public", PRIV, "code-clickhouse"]


def test_flat_token_grant_alone_does_not_fold_restricted_in(world) -> None:
    # The grant puts PRIV into the union; the missing claim takes it out.
    world["grant_rows"] = [PRIV]
    world["grants"][(ORG, PRIV, "alice@x")] = Role.ADMIN
    set_current_auth(_flat(["personal"]))
    assert asyncio.run(resolve_my_namespaces(ORG)) == ["personal"]
    set_current_auth(_flat(["personal", PRIV]))
    assert asyncio.run(resolve_my_namespaces(ORG)) == [PRIV, "personal"]


def test_wildcard_token_never_resolves_restricted(world) -> None:
    world["grant_rows"] = [PRIV]
    world["grants"][(ORG, PRIV, "alice@x")] = Role.ADMIN
    set_current_auth(_flat(["*"], roles=(Role.ADMIN,)))
    assert asyncio.run(resolve_my_namespaces(ORG)) == []


def test_a_wildcard_scope_excludes_a_named_claim_capped_at_a_revoked_grant(
    world, monkeypatch
) -> None:
    """`*` plus a named `x` whose grant was revoked: the claim on `x` is capped
    at nothing live (`authz.claim_cap`), so the gate refuses `x`, and the
    every-namespace scope leaves it out with the restricted ones."""
    from server.auth.context import every_readable_namespace

    async def _revoked(org, subject):
        return grant_cache.GrantMap(ranks={"x": 0})

    monkeypatch.setattr(grant_cache, "_read", _revoked)
    grant_cache.clear()
    set_current_auth(_flat(["*", "x", "shop"]))
    f = asyncio.run(every_readable_namespace(ORG))
    assert f.accessible and f.params["ns_hidden"] == [PRIV, "x"]


def test_org_admin_rollup_excludes_restricted_by_name(world) -> None:
    set_current_auth(_flat(["*"], roles=(Role.ORG_ADMIN,)))
    f = asyncio.run(resolve_org_scope(ORG))
    assert f.accessible and "NOT IN {ns_hidden" in f.where and f.params["ns_hidden"] == [PRIV]
    world["grants"][(ORG, PRIV, "alice@x")] = Role.VIEWER
    grant_cache.clear()
    set_current_auth(_flat([PRIV], roles=(Role.ORG_ADMIN,)))
    f = asyncio.run(resolve_org_scope(ORG))
    assert f.accessible and "ns_hidden" not in f.params, "nothing restricted is hidden now"


def test_org_admin_sees_nothing_when_the_directory_is_unreadable(world, monkeypatch) -> None:
    async def _down(org):
        raise ClassificationUnavailable("down")

    monkeypatch.setattr(nc, "restricted_set", _down)
    set_current_auth(_flat(["*"], roles=(Role.ORG_ADMIN,)))
    assert asyncio.run(resolve_org_scope(ORG)).accessible is False


def test_auth_disabled_resolves_verbatim(world) -> None:
    set_current_auth(None)
    assert asyncio.run(resolve_my_namespaces(ORG, subject="alice@x")) == []
