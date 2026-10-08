"""erasure.list namespace scoping — mirrors the audit.list rule.

A non-admin is narrowed to their own namespaces; a `*`-wildcard / operator
token gets the org-wide view (resolve_my_namespaces strips `*`, so the handler
must short-circuit on the wildcard rather than narrow to an empty set). CH-free.
"""

from __future__ import annotations

import pytest

from server.auth.jwt import AuthInfo
from server.operations.base import Role


def _auth(subject, namespaces, roles):
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
async def test_wildcard_token_gets_org_wide_view(monkeypatch) -> None:
    """Regression guard: a `*`-namespace operator token must not be narrowed
    to nothing — it reaches every namespace, so it sees the org-wide list."""
    import server.auth.context as ctx
    from server.erasure.api.erasure import ErasureListInput
    from server.erasure.app import handlers as h

    monkeypatch.setattr(
        ctx, "current_auth", lambda: _auth("svc", ("*",), (Role.ADMIN, Role.WRITER))
    )

    async def _mine(org, subject=None):  # pragma: no cover - wildcard shouldn't call it
        raise AssertionError("wildcard token must not be narrowed via resolve_my_namespaces")

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)
    import server.core.ch as chmod

    ch = _CH()
    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    out = await h.handle_erasure_list(ErasureListInput(org="dev"))
    assert ch.queried is True
    assert out.rows == []


@pytest.mark.asyncio
async def test_non_admin_with_no_namespaces_is_empty(monkeypatch) -> None:
    """A non-admin, non-wildcard caller with zero accessible namespaces sees
    nothing — and we don't even hit ClickHouse."""
    import server.auth.context as ctx
    from server.erasure.api.erasure import ErasureListInput
    from server.erasure.app import handlers as h

    monkeypatch.setattr(ctx, "current_auth", lambda: _auth("bob", ("bobs-ns",), (Role.VIEWER,)))

    async def _mine(org, subject=None):
        return []

    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)
    import server.core.ch as chmod

    class _Raise(_CH):
        async def query_all(self, *a, **k):
            raise AssertionError("empty-scope request must not hit ClickHouse")

    monkeypatch.setattr(chmod, "get_client", lambda: _Raise())
    out = await h.handle_erasure_list(ErasureListInput(org="dev"))
    assert out.rows == []


class _Recorder(_CH):
    def __init__(self) -> None:
        super().__init__()
        self.sql = ""
        self.params: dict = {}

    async def query_all(self, sql, params=None, **k):
        self.queried = True
        if "loom.erasure_requests" in sql:
            self.sql, self.params = sql, dict(params or {})
        return []


async def _list(monkeypatch, auth, *, hidden=(), mine=()):
    import server.auth.context as ctx
    import server.core.ch as chmod
    from server.erasure.api.erasure import ErasureListInput
    from server.erasure.app import handlers as h

    monkeypatch.setattr(ctx, "current_auth", lambda: auth)

    async def _hidden(org):
        return list(hidden)

    async def _mine(org, subject=None):
        return list(mine)

    monkeypatch.setattr(ctx, "restricted_namespaces_hidden_from", _hidden)
    monkeypatch.setattr(ctx, "resolve_my_namespaces", _mine)
    ch = _Recorder()
    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    await h.handle_erasure_list(ErasureListInput(org="dev"))
    return ch


@pytest.mark.asyncio
async def test_an_org_admin_does_not_see_restricted_namespaces_it_cannot_read(monkeypatch):
    ch = await _list(monkeypatch, _auth("ops", ("*",), (Role.ORG_ADMIN,)), hidden=["vault"])
    assert "scope_value IN {hidden:Array(String)}" in ch.sql and ch.params["hidden"] == ["vault"]
    assert "scope_type = 'namespace' AND scope_value IN {nslist" not in ch.sql  # org-wide view


@pytest.mark.asyncio
async def test_a_scoped_token_never_gets_the_org_wide_view(monkeypatch):
    scoped = AuthInfo(
        subject="sa:bot",
        org="dev",
        namespaces=("mine",),
        roles=(Role.ORG_ADMIN,),
        is_service_account=True,
        ns_roles=(("mine", "viewer"),),
    )
    ch = await _list(monkeypatch, scoped, mine=["mine"])
    assert "scope_type = 'namespace'" in ch.sql and ch.params["nslist"] == ["mine"]


@pytest.mark.asyncio
async def test_a_wildcard_non_admin_sees_namespace_sagas_only(monkeypatch):
    ch = await _list(monkeypatch, _auth("svc", ("*",), (Role.ADMIN,)))
    assert "AND scope_type = 'namespace'" in ch.sql  # user_id sagas are operator-only
