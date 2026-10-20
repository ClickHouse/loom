"""`erasure.export`: a namespace export is authorized against
the namespace itself; a subject export excludes the restricted namespaces
the caller may not read."""

from __future__ import annotations

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.erasure.api.erasure import ErasureExportInput
from server.erasure.app import handlers as h
from server.operations.base import Role

ORG, PRIV, STD = "clickhouse", "tracker-ops-restricted", "support-bot"


@pytest.fixture
def exports(monkeypatch):
    async def _mem(org, scope_type, scope_value, *, include_tombstoned, limit, shown):
        rows = [{"namespace": STD, "value": "ok"}, {"namespace": PRIV, "value": "SECRET"}]
        kept = [r for r in rows if shown.admits(r["namespace"])][:limit]
        return kept, len(kept), False

    async def _empty(org, scope_type, scope_value, *, limit, shown):
        return ([], 0, False)

    monkeypatch.setattr(h, "_export_memory", _mem)
    monkeypatch.setattr(h, "_export_knowledge", _empty)
    monkeypatch.setattr(h, "_export_agent_events", _empty)
    monkeypatch.setattr(h, "_export_audit_log", _empty)

    async def _emit(**kw):
        return "h"

    monkeypatch.setattr(h.audit, "emit", _emit)
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(repos, "configured_repos", list)
    nc.reset()
    nc._cache[ORG] = (1e12, frozenset({PRIV}))
    grants = {}

    async def _grant(org, ns, subject):
        return grants.get((org, ns, subject))

    monkeypatch.setattr(authz, "grant_role_for", _grant)
    yield grants
    nc.reset()
    set_current_auth(None)


def _admin(*names, role=Role.ADMIN):
    return AuthInfo(
        subject="ops@x",
        org=ORG,
        namespaces=tuple(names),
        roles=(role,),
        is_service_account=False,
    )


@pytest.mark.asyncio
async def test_namespace_export_of_a_restricted_namespace_needs_the_two_factors(exports) -> None:
    set_current_auth(_admin("*"))
    with pytest.raises(AuthInsufficientError):
        await h.handle_erasure_export(
            ErasureExportInput(org=ORG, scope_type="namespace", scope_value=PRIV, requester="ops@x")
        )
    exports[(ORG, PRIV, "ops@x")] = Role.ADMIN
    set_current_auth(_admin(PRIV))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="namespace", scope_value=PRIV, requester="ops@x")
    )
    assert out.counts["memory"] == 2


@pytest.mark.asyncio
async def test_subject_export_drops_unreadable_restricted_rows(exports) -> None:
    set_current_auth(_admin("*", role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    rows = out.memory.rows
    assert [r["namespace"] for r in rows] == [STD] and out.counts["memory"] == 1
    exports[(ORG, PRIV, "ops@x")] = Role.VIEWER
    set_current_auth(_admin(PRIV, role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert out.counts["memory"] == 2


@pytest.mark.asyncio
async def test_a_subject_export_needs_org_admin_like_the_erasure(exports) -> None:
    """erasure.request refuses a user_id erasure to a namespace ADMIN; exporting everything
    held about that user across the org is refused to the same caller."""
    from server.core.errors import AuthInsufficientError

    set_current_auth(_admin("*"))
    with pytest.raises(AuthInsufficientError):
        await h.handle_erasure_export(
            ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
        )


@pytest.mark.asyncio
async def test_auth_disabled_exports_everything(exports) -> None:
    set_current_auth(None)
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert out.counts["memory"] == 2


@pytest.mark.asyncio
async def test_the_export_audits_each_high_row_it_serves(exports, monkeypatch) -> None:
    def row(ns: str, key: str, sensitivity: str) -> dict:
        return {
            "namespace": ns,
            "memory_key": key,
            "memory_id": f"id-{key}",
            "version": 1,
            "sensitivity": sensitivity,
            "value": "v",
        }

    async def _mem(*_a, shown, **_k):
        rows = [row(STD, "plain", ""), row(STD, "vault", "high"), row(PRIV, "hidden", "high")]
        kept = [r for r in rows if shown.admits(r["namespace"])]
        return kept, len(kept), False

    audited: list[tuple[str, list[str]]] = []

    async def _emit_many(event, **kw):
        audited.append((kw["namespace"], [s["memory_key"] for s in kw["subjects"]]))

    monkeypatch.setattr(h, "_export_memory", _mem)
    monkeypatch.setattr(h.audit, "emit_many", _emit_many)
    # An org admin with no grant on the restricted namespace: its rows are not served,
    # so they are not audited as read either.
    set_current_auth(_admin("*", role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert [r["memory_key"] for r in out.memory.rows] == ["plain", "vault"]
    assert audited == [(STD, ["vault"])]


@pytest.mark.asyncio
async def test_a_user_export_carries_their_personal_collection_and_no_other_unread_one(
    exports, monkeypatch
) -> None:
    from server.core.principals import personal_name

    mine, theirs = personal_name(ORG, "user:u1"), personal_name(ORG, "user:u2")

    async def _mem(org, scope_type, scope_value, *, include_tombstoned, limit, shown):
        rows = [{"namespace": ns, "value": ns} for ns in (STD, mine, theirs, "col-hr")]
        kept = [r for r in rows if shown.admits(r["namespace"])][:limit]
        return kept, len(kept), False

    async def _readable(auth, org):
        return []

    monkeypatch.setattr(h, "_export_memory", _mem)
    monkeypatch.setattr(h.sharing, "readable", _readable)
    set_current_auth(_admin("*", role=Role.ORG_ADMIN))
    out = await h.handle_erasure_export(
        ErasureExportInput(org=ORG, scope_type="user_id", scope_value="u1", requester="ops@x")
    )
    assert [r["namespace"] for r in out.memory.rows] == [STD, mine]
    assert out.counts["memory"] == 2


def test_an_export_filters_what_it_may_not_show_before_its_limit() -> None:
    """The filter is in the statement, ahead of its LIMIT, so a hidden row never
    takes a visible row's place on the page."""
    import asyncio

    from server.erasure.store import exports

    sent: list[tuple[str, dict]] = []

    class _CH:
        async def query_all(self, sql, params=None, **_k):
            sent.append((sql, params))
            return []

    shown = exports.Shown(hidden=(PRIV,), collections=("col-me-1",))
    for read in (
        exports.export_knowledge(ORG, "user_id", "u1", limit=2, shown=shown, ch=_CH()),
        exports.export_agent_events(ORG, "user_id", "u1", limit=2, shown=shown, ch=_CH()),
        exports.export_audit_by_subject_substring(ORG, "u1", limit=2, shown=shown, ch=_CH()),
        exports.export_memory(
            ORG, "user_id", "u1", include_tombstoned=True, limit=2, shown=shown, ch=_CH()
        ),
    ):
        asyncio.run(read)
    for sql, params in sent:
        assert sql.index("shown_collections") < sql.index("LIMIT"), sql
        assert params["shown_hidden"] == [PRIV] and params["shown_collections"] == ["col-me-1"]
    assert shown.admits(STD) and shown.admits("col-me-1")
    assert not shown.admits(PRIV) and not shown.admits("col-hr")
