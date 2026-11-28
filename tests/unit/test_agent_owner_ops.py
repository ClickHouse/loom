"""An agent settles the conflicts of its own namespace.

`conflicts.resolve` needs ADMIN. An agent holds the owner's admin grant on its own
namespace, and its token names that namespace as a writer's. The transports' gate
(`authorize_op`) admits it to an owner's op there (`agents.owns`) once the ordinary
gate refused the role, while the grant is live. Nothing else widens: not a namespace it
was lent or granted, not another admin op, not a restricted namespace, not a token that
may not write there, not a person, not another org's agent of the same name.

The gate is the real one; the grant cache is faked as one table. Each condition of the
rule has a case that only that condition refuses.
"""

from __future__ import annotations

import dataclasses

import pytest

from server.auth import agents, authz
from server.auth.jwt import AuthInfo
from server.code.app import member_writes
from server.core import grant_cache
from server.operations.base import Role, grant_rank
from server.operations.registry import OPERATIONS_BY_NAME

ORG = "acme"
HOME = "fleet-a"
OTHER = "fleet-b"
RESOLVE = OPERATIONS_BY_NAME["conflicts.resolve"]


def _agent(org: str = ORG) -> AuthInfo:
    """Agent `a` of trust `fleet`, as its exchange mints it, with `fleet-b` as a writer."""
    return AuthInfo(
        subject="sa:fleet:a",
        org=org,
        namespaces=(HOME, OTHER),
        roles=(),
        is_service_account=True,
        ns_roles=((HOME, "writer"), (OTHER, "writer")),
        parent_subjects=("sa:fleet",),
    )


def _person() -> AuthInfo:
    return AuthInfo(
        subject="bob", org=ORG, namespaces=(HOME,), roles=(Role.WRITER,), is_service_account=False
    )


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """(subject, namespace) → the live grant role."""
    table = {
        ("sa:fleet:a", HOME): "admin",
        ("sa:fleet:a", OTHER): "writer",
        ("bob", HOME): "writer",
    }

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        ranks = {ns: grant_rank(role) for (who, ns), role in table.items() if who == subject}
        return grant_cache.GrantMap(ranks=ranks)

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    return table


async def _call(auth: AuthInfo, op: str = "conflicts.resolve", namespace: str = HOME) -> None:
    await member_writes.authorize_op(auth, OPERATIONS_BY_NAME[op], org=ORG, namespace=namespace)


async def test_the_owner_op_needs_admin() -> None:
    assert RESOLVE.required_role is Role.ADMIN
    assert agents.OWNER_OPS == {RESOLVE.name}


async def test_an_agent_resolves_on_its_own_namespace(grants) -> None:
    await _call(_agent())


async def test_an_agent_is_refused_on_a_namespace_it_was_lent(grants) -> None:
    with pytest.raises(PermissionError):
        await _call(_agent(), namespace=OTHER)


async def test_an_admin_grant_on_another_namespace_is_no_ownership(grants) -> None:
    # Only the namespace the subject names is the agent's own.
    grants[("sa:fleet:a", OTHER)] = "admin"
    with pytest.raises(PermissionError):
        await _call(_agent(), namespace=OTHER)


@pytest.mark.parametrize(
    "op",
    ["memory.unlock", "blacklist.remove", "memory.dedup_resolve", "conflicts.allowlist_remove"],
)
async def test_another_admin_op_stays_refused_on_its_own_namespace(grants, op: str) -> None:
    assert OPERATIONS_BY_NAME[op].required_role is Role.ADMIN
    with pytest.raises(PermissionError):
        await _call(_agent(), op)


@pytest.mark.parametrize("role", ["writer", "viewer", ""])
async def test_a_lowered_or_revoked_owner_grant_refuses(grants, role: str) -> None:
    grants[("sa:fleet:a", HOME)] = role  # "": revoked, the row stays at no rank
    with pytest.raises(PermissionError):
        await _call(_agent())


async def test_a_subject_that_never_held_the_grant_is_refused(grants) -> None:
    # No row caps the claim, so the token still writes; the name alone owns nothing.
    del grants[("sa:fleet:a", HOME)]
    await authz.check_authorized(_agent(), Role.WRITER, org=ORG, namespace=HOME)
    with pytest.raises(PermissionError):
        await _call(_agent())


async def test_a_token_that_may_not_write_there_is_refused(grants) -> None:
    read_only_at_home = dataclasses.replace(_agent(), write_namespaces=(OTHER,))
    with pytest.raises(PermissionError):
        await _call(read_only_at_home)


async def test_a_restricted_namespace_is_not_settled_this_way(
    grants, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def restricted(org: str, namespace: str) -> bool:
        return namespace == HOME

    async def direct_grant(org: str, namespace: str, subject: str) -> Role | None:
        return authz.rank_role(grant_rank(grants.get((subject, namespace), "")))

    monkeypatch.setattr(authz, "may_be_restricted", restricted)
    monkeypatch.setattr(authz, "grant_role_for", direct_grant)
    # The restricted gate passes the agent as a writer, and still nothing widens.
    await authz.check_authorized(_agent(), Role.WRITER, org=ORG, namespace=HOME)
    with pytest.raises(PermissionError):
        await _call(_agent())


async def test_a_writer_who_is_no_agent_is_refused(grants) -> None:
    with pytest.raises(PermissionError):
        await _call(_person())


@pytest.mark.parametrize("org", ["other", "*"])
async def test_a_token_of_another_org_or_of_every_org_is_refused(grants, org: str) -> None:
    with pytest.raises(PermissionError):
        await _call(_agent(org=org))


async def test_owns_is_false_without_a_principal_or_a_target(grants) -> None:
    assert not await agents.owns(None, RESOLVE.name, ORG, HOME)
    assert not await agents.owns(_agent(), RESOLVE.name, ORG, None)
    assert not await agents.owns(_agent(), RESOLVE.name, None, HOME)
