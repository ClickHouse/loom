"""Audit-list namespace scoping: a non-admin may not read a namespace they
don't belong to by naming it explicitly — but wildcard/operator tokens and
grantees still can.

audit.list sets skip_namespace_authz=True (so the org-wide namespace=None
fan-out works), so the handler is the only thing standing between a caller and
a foreign namespace's audit trail. These pin: (1) a non-member non-admin is
denied; (2) an admin passes; (3) a `*`-wildcard token passes
(resolve_my_namespaces strips `*`, so the check must also consult
can_access). CH-free.
"""

from __future__ import annotations

import pytest

from server.auth.jwt import AuthInfo
from server.operations.base import Role


def _auth(subject: str, namespaces: tuple[str, ...], roles: tuple[Role, ...]) -> AuthInfo:
    return AuthInfo(
        subject=subject,
        org="dev",
        namespaces=namespaces,
        roles=roles,
        is_service_account=False,
    )


class _CH:
    def __init__(self) -> None:
        self.queried = False

    async def query_all(self, *a, **k):
        self.queried = True
        return []


@pytest.mark.asyncio
async def test_non_admin_cannot_read_foreign_namespace(monkeypatch) -> None:
    import server.auth.context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: _auth("bob", ("bobs-ns",), (Role.VIEWER,)))

    async def _mine(org, subject=None):
        return ["bobs-ns"]  # not the requested namespace

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)

    class _Raise(_CH):
        async def query_all(self, *a, **k):
            raise AssertionError("denied request must not hit ClickHouse")

    monkeypatch.setattr(h, "get_client", lambda: _Raise())
    out = await h.handle_audit_list(AuditListInput(org="dev", namespace="team-private-096049"))
    assert out.rows == []


@pytest.mark.asyncio
async def test_org_admin_is_not_blocked(monkeypatch) -> None:
    import server.auth.context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: _auth("ops", ("own-ns",), (Role.ORG_ADMIN,)))

    async def _mine(org, subject=None):  # pragma: no cover - admin shouldn't need it
        raise AssertionError("admin must not be narrowed")

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)
    ch = _CH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    await h.handle_audit_list(AuditListInput(org="dev", namespace="team-private-096049"))
    assert ch.queried is True


@pytest.mark.asyncio
async def test_wildcard_token_is_not_blocked(monkeypatch) -> None:
    """A `*`-namespace token (operator tooling / the integration
    harness) must pass — resolve_my_namespaces strips `*`, so the check has to
    fall back to can_access, which honours the wildcard."""
    import server.auth.context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(
        ctx, "current_auth", lambda: _auth("svc", ("*",), (Role.ADMIN, Role.WRITER))
    )

    async def _mine(org, subject=None):
        return []  # `*` is stripped → empty; can_access must carry it

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)
    ch = _CH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = await h.handle_audit_list(AuditListInput(org="dev", namespace="any-ns"))
    assert ch.queried is True
    assert out.rows == []


def _restrict(monkeypatch, *namespaces: str) -> None:
    """Classify `namespaces` restricted in the directory, CH-free."""
    from server.identity.app import namespace_classification as nc

    async def _set(org):
        return frozenset(namespaces)

    monkeypatch.setattr(nc, "restricted_set", _set)


@pytest.mark.parametrize(
    "roles",
    [(Role.ADMIN, Role.WRITER), (Role.ORG_ADMIN,)],
    ids=["wildcard-token", "org-admin"],
)
@pytest.mark.asyncio
async def test_restricted_namespace_is_hidden_when_named(monkeypatch, roles) -> None:
    """A restricted namespace is read only by a token that names it AND a
    direct grant. The org-wide branch hides it (resolve_org_scope); naming it
    explicitly must not be the way round that — a `*` token or an org admin
    gets the same empty page an inaccessible namespace gets."""
    import server.auth.context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    _restrict(monkeypatch, "secret-ns")
    monkeypatch.setattr(ctx, "current_auth", lambda: _auth("svc", ("*",), roles))

    async def _mine(org, subject=None):
        return []

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)

    class _Raise(_CH):
        async def query_all(self, *a, **k):
            raise AssertionError("a hidden restricted namespace must not hit ClickHouse")

    monkeypatch.setattr(h, "get_client", lambda: _Raise())
    out = await h.handle_audit_list(AuditListInput(org="dev", namespace="secret-ns"))
    assert out.rows == [] and out.next_cursor is None


@pytest.mark.asyncio
async def test_restricted_namespace_reads_for_a_named_token_with_a_grant(monkeypatch) -> None:
    """The two-factor reader (token names it + direct grant) still reads it."""
    import server.auth.authz as authz
    import server.auth.context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    _restrict(monkeypatch, "secret-ns")
    monkeypatch.setattr(ctx, "current_auth", lambda: _auth("ann", ("secret-ns",), (Role.VIEWER,)))

    async def _grant(org, namespace, subject):
        return Role.VIEWER

    monkeypatch.setattr(authz, "grant_role_for", _grant)
    ch = _CH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    await h.handle_audit_list(AuditListInput(org="dev", namespace="secret-ns"))
    assert ch.queried is True
