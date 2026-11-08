"""Restricted namespaces: a read needs a token that names the
namespace AND a live direct grant. Every widening rule — org-readable code,
visibility=public, wildcard claims, service-account inheritance, platform
roles — is off there, and the directory lookup fails closed."""

from __future__ import annotations

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.directory import ClassificationUnavailable
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "clickhouse"
NS = "code-example-private"


def _flat(namespaces=(NS,), roles=(Role.VIEWER,), org=ORG, subject="alice@x") -> AuthInfo:
    return AuthInfo(
        subject=subject,
        org=org,
        namespaces=tuple(namespaces),
        roles=tuple(roles),
        is_service_account=False,
    )


def _scoped(
    ns_roles=((NS, "viewer"),), namespaces=(NS,), org=ORG, subject="sa:clickgap"
) -> AuthInfo:
    return AuthInfo(
        subject=subject,
        org=org,
        namespaces=tuple(namespaces),
        roles=(),
        is_service_account=True,
        ns_roles=tuple(ns_roles),
    )


@pytest.fixture
def restricted(monkeypatch: pytest.MonkeyPatch):
    """`NS` is restricted by the directory row; grants come from `state`."""
    import server.code.domain.index.repos as repos

    state = {
        "directory": frozenset({NS}),
        "config": {},  # (org, ns) -> classification
        "grants": {},  # (org, ns, subject) -> Role
        "org_readable": False,
        "unavailable": False,
        "serve": True,
    }

    class _Cfg:
        def __init__(self, classification: str, org_readable: bool) -> None:
            self.classification = classification
            self.org_readable = org_readable

    def _repo_for(org, ns):
        cls = state["config"].get((org, ns))
        if cls is None and (org, ns) == (ORG, "code-clickhouse"):
            return _Cfg("standard", state["org_readable"])
        return _Cfg(cls, state["org_readable"]) if cls else None

    monkeypatch.setattr(repos, "config_entry", _repo_for)

    async def _restricted_set(org):
        if state["unavailable"]:
            raise ClassificationUnavailable("ch down")
        return state["directory"] if org == ORG else frozenset()

    monkeypatch.setattr(nc, "restricted_set", _restricted_set)

    async def _grant(org, ns, subject):
        return state["grants"].get((org, ns, subject))

    monkeypatch.setattr(authz, "grant_role_for", _grant)

    async def _creator(org, sa, **_kw):  # inheritance must never be consulted for NS
        return "alice@x"

    monkeypatch.setattr("server.auth.service_accounts.inheriting_creator", _creator)
    import server.settings as st

    monkeypatch.setattr(st.settings, "restricted_serve", True)
    state["settings"] = st.settings
    return state


async def _denied(auth, role=Role.VIEWER, ns=NS):
    with pytest.raises(PermissionError) as ei:
        await authz.check_authorized(auth, role, org=ORG, namespace=ns)
    return str(ei.value)


# --- the two factors ---------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_and_direct_grant_read(restricted) -> None:
    restricted["grants"][(ORG, NS, "alice@x")] = Role.VIEWER
    await authz.check_authorized(_flat(), Role.VIEWER, org=ORG, namespace=NS)


@pytest.mark.asyncio
async def test_claim_without_grant_is_refused(restricted) -> None:
    msg = await _denied(_flat())
    assert "cannot access" in msg and "grant" not in msg  # generic text, no cause


@pytest.mark.asyncio
async def test_grant_without_claim_is_refused(restricted) -> None:
    restricted["grants"][(ORG, NS, "alice@x")] = Role.ADMIN
    await _denied(_flat(namespaces=("personal",)))


@pytest.mark.asyncio
async def test_wildcard_claim_never_counts(restricted) -> None:
    restricted["grants"][(ORG, NS, "alice@x")] = Role.ADMIN
    await _denied(_flat(namespaces=("*",), roles=(Role.ADMIN,)))


@pytest.mark.asyncio
async def test_scoped_token_naming_the_namespace_with_grant_reads(restricted) -> None:
    restricted["grants"][(ORG, NS, "sa:clickgap")] = Role.VIEWER
    await authz.check_authorized(_scoped(), Role.VIEWER, org=ORG, namespace=NS)


@pytest.mark.asyncio
async def test_scoped_token_role_is_still_the_ceiling(restricted) -> None:
    restricted["grants"][(ORG, NS, "sa:clickgap")] = Role.ADMIN
    await _denied(_scoped(), Role.WRITER)  # ns_roles says viewer


@pytest.mark.asyncio
async def test_grant_below_required_role_is_refused(restricted) -> None:
    restricted["grants"][(ORG, NS, "alice@x")] = Role.VIEWER
    await _denied(_flat(roles=(Role.WRITER,)), Role.WRITER)


@pytest.mark.asyncio
async def test_service_account_does_not_inherit_creator_grant(restricted) -> None:
    restricted["grants"][(ORG, NS, "alice@x")] = Role.ADMIN  # the creator's grant
    await _denied(_flat(subject="sa:agent-1"))


@pytest.mark.asyncio
async def test_other_org_is_refused(restricted) -> None:
    restricted["grants"][(ORG, NS, "alice@x")] = Role.ADMIN
    await _denied(_flat(org="acme"))
    await _denied(_flat(org="*"))


@pytest.mark.asyncio
async def test_platform_admin_has_no_content_bypass(restricted) -> None:
    await _denied(_flat(namespaces=("*",), roles=(Role.PLATFORM_ADMIN,)))
    restricted["grants"][(ORG, NS, "alice@x")] = Role.VIEWER
    await authz.check_authorized(
        _flat(roles=(Role.PLATFORM_ADMIN,)), Role.VIEWER, org=ORG, namespace=NS
    )


# --- widening rules are switched off -----------------------------------------


@pytest.mark.asyncio
async def test_org_readable_code_rule_does_not_apply(restricted) -> None:
    restricted["config"][(ORG, NS)] = "restricted"
    restricted["org_readable"] = True
    await _denied(
        _scoped(ns_roles=(("clickgap-public", "writer"),), namespaces=("clickgap-public",))
    )
    assert not await authz.is_org_readable_code_namespace(ORG, NS)


@pytest.mark.asyncio
async def test_config_restricted_wins_over_a_standard_directory_row(restricted) -> None:
    restricted["directory"] = frozenset()
    restricted["config"][(ORG, NS)] = "restricted"
    await _denied(_flat(roles=(Role.ADMIN,)))
    restricted["grants"][(ORG, NS, "alice@x")] = Role.VIEWER
    await authz.check_authorized(_flat(), Role.VIEWER, org=ORG, namespace=NS)


@pytest.mark.asyncio
async def test_standard_namespaces_keep_the_ordinary_rules(restricted) -> None:
    # The same flat viewer reads its own standard namespace with no grant.
    await authz.check_authorized(
        _flat(namespaces=("personal",)), Role.VIEWER, org=ORG, namespace="personal"
    )


# --- fail closed --------------------------------------------------------------


@pytest.mark.asyncio
async def test_directory_unavailable_denies(restricted) -> None:
    restricted["unavailable"] = True
    restricted["grants"][(ORG, NS, "alice@x")] = Role.ADMIN
    await _denied(_flat(roles=(Role.ADMIN,)))


@pytest.mark.asyncio
async def test_kill_switch_refuses_everyone(restricted, monkeypatch) -> None:
    restricted["grants"][(ORG, NS, "alice@x")] = Role.ADMIN
    monkeypatch.setattr(restricted["settings"], "restricted_serve", False)
    await _denied(_flat(roles=(Role.ADMIN,)))


@pytest.mark.asyncio
async def test_auth_disabled_is_a_no_op(restricted) -> None:
    await authz.check_authorized(None, Role.ADMIN, org=ORG, namespace=NS)


@pytest.mark.asyncio
async def test_row_in_scope_hides_the_row(restricted, monkeypatch) -> None:
    import server.auth.context as ctx

    monkeypatch.setattr(ctx, "current_auth", lambda: _flat())
    assert await authz.row_in_scope(ORG, NS, Role.VIEWER) is False
    restricted["grants"][(ORG, NS, "alice@x")] = Role.VIEWER
    assert await authz.row_in_scope(ORG, NS, Role.VIEWER) is True


def test_names_namespace_is_literal() -> None:
    assert authz.names_namespace(_flat(namespaces=(NS,)), ORG, NS)
    assert authz.names_namespace(_scoped(namespaces=()), ORG, NS)  # ns_roles names it
    assert not authz.names_namespace(_flat(namespaces=("*",)), ORG, NS)
    assert not authz.names_namespace(_flat(namespaces=(NS,), org="acme"), ORG, NS)
