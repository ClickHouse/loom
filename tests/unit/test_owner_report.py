"""The owner's view of their memory — the three statements are
scoped to the owner and the token's namespaces; the handler binds the user
the way own_user reads do."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server.auth.jwt import AuthInfo
from server.memory.app import owner_report as R
from server.operations.base import Role


def test_queries_are_owner_scoped_and_namespace_bounded() -> None:
    (rows, reads, usage), params = R.build_queries(
        org="acme",
        user_id="u1",
        scope=R.namespace_scope(["support", "shop"]),
        since_hours=48,
        limit=20,
    )
    for sql, _ in (rows, reads, usage):
        assert "user_id = {uid:String}" in sql and "status = 'active'" in sql
        assert "tombstoned_at IS NULL" in sql and "loom.memory FINAL" in sql
        assert "namespace IN {nss:Array(String)}" in sql
    assert (
        "ARRAY JOIN e.returned_keys AS k" in reads[0]
        and "e.namespace IN {nss:Array(String)}" in reads[0]
    )
    assert "loom.memory_usage_totals" in usage[0]
    assert params == {
        "org": "acme",
        "uid": "u1",
        "hours": 48,
        "lim": 20,
        "nss": ["shop", "support"],
    }


def test_an_exclusion_scope_reaches_every_statement() -> None:
    scope = R.NamespaceScope(
        " AND namespace NOT IN {ns_hidden:Array(String)}",
        " AND e.namespace NOT IN {ns_hidden:Array(String)}",
        {"ns_hidden": ["hr"]},
    )
    (rows, reads, usage), params = R.build_queries(
        org="acme", user_id="u1", scope=scope, since_hours=1, limit=1
    )
    assert params["ns_hidden"] == ["hr"] and "nss" not in params
    for sql, _ in (rows, reads, usage):
        assert " namespace NOT IN {ns_hidden:Array(String)}" in sql
    assert "e.namespace NOT IN {ns_hidden:Array(String)}" in reads[0]


@pytest.mark.asyncio
async def test_with_auth_off_the_report_reads_every_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api import models_exposure as OPS
    from server.memory.app import exposure as H

    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.auth.context.current_auth", lambda: None)
    await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme", user_id="u1"))
    assert len(ch.calls) == 3 and all("namespace IN" not in sql for sql, _ in ch.calls)


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
        self.calls.append((sql, params or {}))
        if "memory_usage_totals" in sql:
            return [
                {
                    "namespace": "shop",
                    "recalls": 12,
                    "used": 3,
                    "harm": 0,
                    "last_recalled": datetime(2026, 9, 12, tzinfo=UTC),
                }
            ]
        if "ARRAY JOIN" in sql:
            return [
                {
                    "namespace": "shop",
                    "agent": "webshop",
                    "actor": "sa:bot",
                    "calls": 7,
                    "distinct_keys": 4,
                    "last_read": datetime(2026, 9, 12, tzinfo=UTC),
                }
            ]
        return [
            {"namespace": "shop", "memory_type": "semantic", "n": 9},
            {"namespace": "shop", "memory_type": "preference", "n": 2},
        ]


def _token(namespaces: tuple[str, ...], org: str = "acme") -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org=org,
        namespaces=namespaces,
        roles=(Role.VIEWER,),
        is_service_account=True,
        read_user_id="u1",
        read_scope="own_user",
    )


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """The caller's grants, by namespace: what resolve_my_namespaces adds to
    the claim, and what the canonical check falls back to."""
    held: dict[str, list[str]] = {"acme": []}

    async def _resolve(org: str, subject: str | None = None) -> list[str]:
        from server.auth.context import current_auth

        auth = current_auth()
        claim = [n for n in auth.namespaces if n != "*"] if auth else []
        return sorted(set(claim) | set(held.get(org, [])))

    async def _grant(org: str, ns: str, subject: str) -> Role | None:
        return Role.VIEWER if ns in held.get(org, []) else None

    async def _plain(org: str, ns: str) -> bool:
        return False

    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _resolve)
    monkeypatch.setattr("server.auth.authz._lookup_grant", _grant)
    monkeypatch.setattr("server.auth.authz.may_be_restricted", _plain)
    return held


@pytest.mark.asyncio
async def test_handler_binds_the_pinned_user_and_sums_rows(
    monkeypatch: pytest.MonkeyPatch, grants: dict[str, list[str]]
) -> None:
    from server.memory.api import models_exposure as OPS
    from server.memory.app import exposure as H

    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    auth = _token(("shop",))
    monkeypatch.setattr("server.auth.context.current_auth", lambda: auth)
    out = await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme"))
    assert out.user_id == "u1" and out.live_rows == 11
    assert out.reads[0].actor == "sa:bot" and out.reads[0].distinct_keys == 4
    assert out.usage[0].recalls == 12 and out.usage[0].harm == 0
    assert all(p["uid"] == "u1" and p["nss"] == ["shop"] for _, p in ch.calls)
    # a pinned token refuses another user
    with pytest.raises(PermissionError):
        await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme", user_id="u2"))
    # a namespace outside the token's claim (and grants) is refused
    with pytest.raises(PermissionError):
        await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme", namespace="hr"))


@pytest.mark.asyncio
async def test_a_grant_only_token_is_bounded_to_its_grants(
    monkeypatch: pytest.MonkeyPatch, grants: dict[str, list[str]]
) -> None:
    from server.memory.api import models_exposure as OPS
    from server.memory.app import exposure as H

    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    auth = _token(())
    monkeypatch.setattr("server.auth.context.current_auth", lambda: auth)
    # No grants: nothing to report, and no statement runs unbounded.
    out = await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme"))
    assert out.live_rows == 0 and out.reads == [] and ch.calls == []
    with pytest.raises(PermissionError):
        await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme", namespace="hr"))
    assert ch.calls == []
    # A grant on shop: the report covers shop, and only shop.
    grants["acme"] = ["shop"]
    await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme"))
    assert ch.calls and all(p["nss"] == ["shop"] for _, p in ch.calls)
    ch.calls.clear()
    await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme", namespace="shop"))
    assert ch.calls and all(p["nss"] == ["shop"] for _, p in ch.calls)


@pytest.mark.asyncio
async def test_another_tenant_cannot_name_this_orgs_namespace(
    monkeypatch: pytest.MonkeyPatch, grants: dict[str, list[str]]
) -> None:
    from server.memory.api import models_exposure as OPS
    from server.memory.app import exposure as H

    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    grants["acme"] = ["shop"]  # held by a same-named subject in acme
    auth = _token(("*",), org="other")
    monkeypatch.setattr("server.auth.context.current_auth", lambda: auth)
    with pytest.raises(PermissionError):
        await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme", namespace="shop"))
    assert ch.calls == []


@pytest.mark.asyncio
async def test_unpinned_token_must_name_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import IdentityIncompleteError
    from server.memory.api import models_exposure as OPS
    from server.memory.app import exposure as H

    monkeypatch.setattr(
        "server.auth.context.current_auth",
        lambda: SimpleNamespace(read_user_id="", read_scope="namespace", namespaces=("*",)),
    )
    with pytest.raises(IdentityIncompleteError):
        await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme"))


@pytest.mark.asyncio
async def test_a_wildcard_token_leaves_out_the_restricted_namespaces_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`*` never counts toward a restricted namespace (`authz.names_namespace`),
    so the report for a wildcard token excludes one it holds no grant on."""
    import server.code.domain.index.repos as repos
    import server.identity.app.namespace_classification as nc
    from server.memory.api import models_exposure as OPS
    from server.memory.app import exposure as H

    async def _restricted_set(org: str) -> frozenset[str]:
        return frozenset({"ns-r"})

    async def _no_grant(org: str, ns: str, subject: str) -> Role | None:
        return None

    async def _no_collections(auth: AuthInfo, org: str) -> list[str]:
        return []

    monkeypatch.setattr(nc, "restricted_set", _restricted_set)
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: None)
    monkeypatch.setattr(repos, "configured_repos", list)
    monkeypatch.setattr("server.auth.authz._lookup_grant", _no_grant)
    monkeypatch.setattr("server.auth.authz.grant_role_for", _no_grant)
    monkeypatch.setattr("server.auth.sharing.readable", _no_collections)
    monkeypatch.setattr("server.settings.settings.restricted_serve", True, raising=False)
    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    auth = _token(("*",))
    monkeypatch.setattr("server.auth.context.current_auth", lambda: auth)
    await H.handle_memory_owner_report(OPS.MemoryOwnerReportInput(org="acme"))
    assert len(ch.calls) == 3
    for sql, params in ch.calls:
        assert "NOT IN {ns_hidden:Array(String)}" in sql and params["ns_hidden"] == ["ns-r"]
