"""Audit-list namespace scoping: a named namespace takes the same gate a read
of it would (`row_in_scope`), so a non-member, a collection the caller holds no
grant on and a delegated token whose creator is gone all get an empty page —
while wildcard/operator tokens and grantees still read.

audit.list sets skip_namespace_authz=True (so the org-wide namespace=None
fan-out works), so the handler is the only thing standing between a caller and
a foreign namespace's audit trail. CH-free.
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

    monkeypatch.setattr(ctx, "current_auth", lambda: _auth("ops", ("*",), (Role.ORG_ADMIN,)))
    ch = _CH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    await h.handle_audit_list(AuditListInput(org="dev", namespace="team-private-096049"))
    assert ch.queried is True


class _Raise(_CH):
    async def query_all(self, *a, **k):
        raise AssertionError("a refused namespace must not hit ClickHouse")


@pytest.mark.parametrize(
    "roles",
    [(Role.ADMIN, Role.WRITER), (Role.ORG_ADMIN,)],
    ids=["wildcard-token", "org-admin"],
)
@pytest.mark.parametrize("namespace", ["col-hr", "col-me-bob"])
@pytest.mark.asyncio
async def test_a_collection_without_a_grant_is_hidden_when_named(
    monkeypatch, roles, namespace
) -> None:
    """A collection, an end user's personal one included, is reached only through
    its grants: neither a `*` token nor an org admin reads its audit trail by
    naming it, as the org-wide branch leaves it out."""
    import server.auth.context as ctx
    import server.auth.sharing as sharing
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: _auth("svc", ("*",), roles))

    async def _no_role(auth, org, ns):
        return None

    monkeypatch.setattr(sharing, "role", _no_role)
    monkeypatch.setattr(h, "get_client", lambda: _Raise())
    out = await h.handle_audit_list(AuditListInput(org="dev", namespace=namespace))
    assert out.rows == [] and out.next_cursor is None


@pytest.mark.asyncio
async def test_a_delegated_token_of_a_disabled_creator_is_refused(monkeypatch) -> None:
    """A delegated token stops with its creator's account, here as at the gate."""
    import server.auth.context as ctx
    import server.auth.delegation as delegation
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    delegated = AuthInfo(
        subject="sa:bot",
        org="dev",
        namespaces=("*",),
        roles=(Role.VIEWER,),
        is_service_account=True,
        creator="alice",
    )
    monkeypatch.setattr(ctx, "current_auth", lambda: delegated)

    async def _inactive(org, creator):
        return False

    monkeypatch.setattr(delegation, "creator_account_active", _inactive)
    monkeypatch.setattr(h, "get_client", lambda: _Raise())
    out = await h.handle_audit_list(AuditListInput(org="dev", namespace="any-ns"))
    assert out.rows == [] and out.next_cursor is None


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


def _note_access(monkeypatch) -> list[tuple[str, str, str]]:
    """(namespace, op, transport) of each restricted-access audit row emitted."""
    from server.identity.app import namespace_classification as nc
    from server.settings import settings

    noted: list[tuple[str, str, str]] = []

    async def _emit(event_type, **kw):
        assert event_type == "namespace.restricted_access"
        noted.append((kw["namespace"], kw["subject"]["op"], kw["subject"]["transport"]))
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    monkeypatch.setattr(settings, "restricted_audit_inline", True)
    monkeypatch.setattr(nc, "is_restricted_cached", lambda org, ns: ns == "secret-ns")
    return noted


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
    noted = _note_access(monkeypatch)
    out = await h.handle_audit_list(AuditListInput(org="dev", namespace="secret-ns"))
    assert out.rows == [] and out.next_cursor is None
    assert noted == []


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
    noted = _note_access(monkeypatch)
    ch = _CH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    await h.handle_audit_list(AuditListInput(org="dev", namespace="secret-ns"))
    assert ch.queried is True
    assert noted == [("secret-ns", "audit.list", "op")]  # the access row a read of it writes


@pytest.mark.asyncio
async def test_an_org_admin_with_a_listed_claim_reads_the_same_span_both_ways(monkeypatch) -> None:
    """An org admin whose claim lists namespaces reads, org-wide, the namespaces its
    claim and grants reach, the span naming one admits it to; a `*` claim reads
    the org."""
    import server.auth.authz as authz
    import server.auth.context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    class _Capture(_CH):
        def __init__(self) -> None:
            super().__init__()
            self.params: dict = {}
            self.sql = ""

        async def query_all(self, sql, params=None, **k):
            self.sql, self.params = sql, dict(params or {})
            return []

    async def _mine(org, subject=None):
        return ["own-ns"]

    async def _no_cap(*a, **k):
        return None

    async def _no_grant(org, namespace, subject):
        return None

    async def _hidden(org):
        return []

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)
    monkeypatch.setattr(ctx, "restricted_namespaces_hidden_from", _hidden)
    monkeypatch.setattr(authz, "claim_cap", _no_cap)
    monkeypatch.setattr(authz, "grant_role_for", _no_grant)
    listed = _auth("ops", ("own-ns",), (Role.ORG_ADMIN,))
    monkeypatch.setattr(ctx, "current_auth", lambda: listed)
    ch = _Capture()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    # Org-wide: the member span.
    await h.handle_audit_list(AuditListInput(org="dev"))
    assert "namespace IN {ns_list:Array(String)}" in ch.sql and ch.params["ns_list"] == ["own-ns"]
    # Named: the same span, its own namespace read and another refused.
    ch.sql = ""
    await h.handle_audit_list(AuditListInput(org="dev", namespace="own-ns"))
    assert ch.params.get("ns") == "own-ns"
    ch.sql = ""
    out = await h.handle_audit_list(AuditListInput(org="dev", namespace="other-ns"))
    assert out.rows == [] and ch.sql == ""
    # A `*` claim: the whole org, no member list.
    wildcard = _auth("ops", ("*",), (Role.ORG_ADMIN,))
    monkeypatch.setattr(ctx, "current_auth", lambda: wildcard)
    await h.handle_audit_list(AuditListInput(org="dev"))
    assert "ns_list" not in ch.params and "{ns_list:Array(String)}" not in ch.sql
