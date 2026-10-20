"""Who may request, export, read the status of and list erasures, pinned.

Every principal below meets every scope of the four erasure handlers. The
answer is reduced to one word: `ok` (the handler went on to the saga or the
read), `403` (AUTH_INSUFFICIENT, raised as a LoomServerError or an
HTTPException; a bare PermissionError out of a handler would be a 500 at the
HTTP transport and reads `perm`), `hidden`
(erasure.status answered found=False), or for erasure.list the scope of rows
it read: `none` (no query), `org` (every scope type), `ns:*` (every namespace
saga) or `ns:[...]` (the sagas of those namespaces).

The transport's own gate (WRITER for request, ADMIN for export, VIEWER for
status and list, all bound to the body's org) runs before these handlers;
this matrix is the handler half only. No grants exist and nothing is
restricted, so only the token decides. CH-free.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import LoomServerError
from server.erasure.api.erasure import (
    ErasureExportInput,
    ErasureListInput,
    ErasureRequestInput,
    ErasureStatusInput,
)
from server.erasure.app import handlers
from server.erasure.store import exports as erasure_exports
from server.erasure.store import ledger as erasure_ledger
from server.operations.base import Role


def _flat(role: Role, *, org: str = "o", namespaces: tuple[str, ...] = ("A",)) -> AuthInfo:
    return AuthInfo(
        subject="alice", org=org, namespaces=namespaces, roles=(role,), is_service_account=False
    )


PRINCIPALS: dict[str, AuthInfo] = {
    "viewer": _flat(Role.VIEWER),
    "writer": _flat(Role.WRITER),
    "admin": _flat(Role.ADMIN),
    "wildcard_viewer": _flat(Role.VIEWER, namespaces=("*",)),
    "org_admin": _flat(Role.ORG_ADMIN),
    "org_admin_other_org": _flat(Role.ORG_ADMIN, org="other"),
    "org_admin_wildcard_org": _flat(Role.ORG_ADMIN, org="*"),
    "platform_admin": _flat(Role.PLATFORM_ADMIN, org="other"),
    # Flat ORG_ADMIN, but its authority is the per-namespace map: admin on A.
    "scoped": AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("A",),
        roles=(Role.ORG_ADMIN,),
        is_service_account=True,
        ns_roles=(("A", "admin"),),
    ),
    "empty_subject": AuthInfo(
        subject="", org="o", namespaces=("A",), roles=(Role.VIEWER,), is_service_account=False
    ),
}

SCOPES: dict[str, tuple[str, str]] = {
    "ns_own": ("namespace", "A"),
    "ns_other": ("namespace", "B"),
    "user_id": ("user_id", "u1"),
    "ns_list": ("namespace_list", '["A"]'),
}


class _Reached(Exception):
    """The request passed the handler's authz and reached the saga."""


class _ListCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[Any]:
        self.calls.append((sql, dict(params or {})))
        return []


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def _no_grant(org: str, ns: str, subject: str) -> None:
        return None

    async def _false(*a: Any, **kw: Any) -> bool:
        return False

    async def _begin(*a: Any, **kw: Any) -> Any:
        raise _Reached

    async def _rows(*a: Any, **kw: Any) -> list[Any]:
        return []

    async def _noop(*a: Any, **kw: Any) -> None:
        return None

    async def _none_hidden(org: str) -> list[str]:
        return []

    async def _mine(org: str, subject: str | None = None) -> list[str]:
        from server.auth.context import current_auth

        auth = current_auth()
        return [n for n in (auth.namespaces if auth else ()) if n != "*"]

    monkeypatch.setattr(authz, "_lookup_grant", _no_grant)
    monkeypatch.setattr(authz, "may_be_restricted", _false)
    monkeypatch.setattr(authz, "_is_org_readable_code_namespace", _false)
    monkeypatch.setattr(handlers, "begin_erasure", _begin)
    for name in (
        "export_memory",
        "export_knowledge",
        "export_agent_events",
        "export_audit_namespace",
        "export_audit_by_subject_substring",
    ):
        monkeypatch.setattr(erasure_exports, name, _rows)
    monkeypatch.setattr("server.observability.audit.emit", _noop)
    monkeypatch.setattr("server.auth.context.restricted_namespaces_hidden_from", _none_hidden)
    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _mine)
    ch = _ListCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    yield ch
    set_current_auth(None)


async def _outcome(call: Any) -> str:
    try:
        await call
    except _Reached:
        return "ok"
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {}
        return "403" if e.status_code == 403 and detail.get("code") == "AUTH_INSUFFICIENT" else "?"
    except LoomServerError as e:
        return "403" if e.status_code == 403 else e.code
    except PermissionError:
        return "perm"
    return "ok"


async def _request(scope: str) -> str:
    scope_type, scope_value = SCOPES[scope]
    return await _outcome(
        handlers.handle_erasure_request(
            ErasureRequestInput(
                org="o", scope_type=scope_type, scope_value=scope_value, requester="r"
            )
        )
    )


async def _export(scope: str) -> str:
    scope_type, scope_value = SCOPES[scope]
    return await _outcome(
        handlers.handle_erasure_export(
            ErasureExportInput(
                org="o", scope_type=scope_type, scope_value=scope_value, requester="r"
            )
        )
    )


async def _status(scope: str, monkeypatch: pytest.MonkeyPatch) -> str:
    scope_type, scope_value = SCOPES[scope]

    async def _row(erasure_id: str) -> dict[str, Any]:
        return {
            "id": erasure_id,
            "org": "o",
            "scope_type": scope_type,
            "scope_value": scope_value,
            "requester": "r",
            "status": "completed",
            "current_step": 99,
            "requested_at": None,
            "completed_at": None,
            "failure_reason": "",
        }

    monkeypatch.setattr(erasure_ledger, "fetch_request_status", _row)
    out = await handlers.handle_erasure_status(ErasureStatusInput(erasure_id="e1"))
    return "ok" if out.found else "hidden"


async def _list(ch: _ListCH) -> str:
    ch.calls.clear()
    await handlers.handle_erasure_list(ErasureListInput(org="o"))
    if not ch.calls:
        return "none"
    sql, params = ch.calls[-1]
    if "nslist" in params:
        return f"ns:{params['nslist']}"
    return "ns:*" if "scope_type = 'namespace'" in sql else "org"


# A namespace erasure needs admin on the target; user_id and namespace_list
# need the org-wide view (a flat ORG_ADMIN of this org or of every org, or a
# PLATFORM_ADMIN). An empty subject is checked like any other principal.
REQUEST: dict[str, dict[str, str]] = {
    "admin": {"ns_own": "ok", "ns_other": "403", "user_id": "403", "ns_list": "403"},
    "empty_subject": {"ns_own": "403", "ns_other": "403", "user_id": "403", "ns_list": "403"},
    "org_admin": {"ns_own": "ok", "ns_other": "ok", "user_id": "ok", "ns_list": "ok"},
    "org_admin_other_org": {"ns_own": "403", "ns_other": "403", "user_id": "403", "ns_list": "403"},
    "org_admin_wildcard_org": {"ns_own": "ok", "ns_other": "ok", "user_id": "ok", "ns_list": "ok"},
    "platform_admin": {"ns_own": "ok", "ns_other": "ok", "user_id": "ok", "ns_list": "ok"},
    "scoped": {"ns_own": "ok", "ns_other": "403", "user_id": "403", "ns_list": "403"},
    "viewer": {"ns_own": "403", "ns_other": "403", "user_id": "403", "ns_list": "403"},
    "wildcard_viewer": {"ns_own": "403", "ns_other": "403", "user_id": "403", "ns_list": "403"},
    "writer": {"ns_own": "403", "ns_other": "403", "user_id": "403", "ns_list": "403"},
}
# A namespace export is checked against the namespace; a subject export needs
# the org-wide view.
EXPORT: dict[str, dict[str, str]] = {
    "admin": {"ns_own": "ok", "ns_other": "403", "user_id": "403"},
    "empty_subject": {"ns_own": "403", "ns_other": "403", "user_id": "403"},
    "org_admin": {"ns_own": "ok", "ns_other": "403", "user_id": "ok"},
    "org_admin_other_org": {"ns_own": "403", "ns_other": "403", "user_id": "403"},
    "org_admin_wildcard_org": {"ns_own": "ok", "ns_other": "403", "user_id": "ok"},
    "platform_admin": {"ns_own": "403", "ns_other": "403", "user_id": "ok"},
    "scoped": {"ns_own": "ok", "ns_other": "403", "user_id": "403"},
    "viewer": {"ns_own": "403", "ns_other": "403", "user_id": "403"},
    "wildcard_viewer": {"ns_own": "403", "ns_other": "403", "user_id": "403"},
    "writer": {"ns_own": "403", "ns_other": "403", "user_id": "403"},
}
# A namespace saga needs VIEWER on its namespace; every other scope needs
# flat membership of the saga's org. A refusal is found=False.
STATUS: dict[str, dict[str, str]] = {
    "admin": {"ns_own": "ok", "ns_other": "hidden", "user_id": "ok", "ns_list": "ok"},
    "empty_subject": {"ns_own": "ok", "ns_other": "hidden", "user_id": "ok", "ns_list": "ok"},
    "org_admin": {"ns_own": "ok", "ns_other": "hidden", "user_id": "ok", "ns_list": "ok"},
    "org_admin_other_org": {
        "ns_own": "hidden",
        "ns_other": "hidden",
        "user_id": "hidden",
        "ns_list": "hidden",
    },
    "org_admin_wildcard_org": {
        "ns_own": "ok",
        "ns_other": "hidden",
        "user_id": "ok",
        "ns_list": "ok",
    },
    "platform_admin": {
        "ns_own": "hidden",
        "ns_other": "hidden",
        "user_id": "hidden",
        "ns_list": "hidden",
    },
    "scoped": {"ns_own": "ok", "ns_other": "hidden", "user_id": "hidden", "ns_list": "hidden"},
    "viewer": {"ns_own": "ok", "ns_other": "hidden", "user_id": "ok", "ns_list": "ok"},
    "wildcard_viewer": {"ns_own": "ok", "ns_other": "ok", "user_id": "ok", "ns_list": "ok"},
    "writer": {"ns_own": "ok", "ns_other": "hidden", "user_id": "ok", "ns_list": "ok"},
}
# The org-wide view is a flat ORG_ADMIN of this org or a PLATFORM_ADMIN; a
# wildcard flat token sees every namespace saga; everyone else, their own.
LIST: dict[str, str] = {
    "admin": "ns:['A']",
    "empty_subject": "ns:['A']",
    "org_admin": "org",
    "org_admin_other_org": "ns:['A']",
    "org_admin_wildcard_org": "org",
    "platform_admin": "org",
    "scoped": "ns:['A']",
    "viewer": "ns:['A']",
    "wildcard_viewer": "ns:*",
    "writer": "ns:['A']",
}


@pytest.mark.parametrize("who", sorted(PRINCIPALS))
async def test_erasure_request_matrix(world: Any, who: str) -> None:
    set_current_auth(PRINCIPALS[who])
    assert {s: await _request(s) for s in SCOPES} == REQUEST[who]


@pytest.mark.parametrize("who", sorted(PRINCIPALS))
async def test_erasure_export_matrix(world: Any, who: str) -> None:
    set_current_auth(PRINCIPALS[who])
    scopes = [s for s in SCOPES if s != "ns_list"]
    assert {s: await _export(s) for s in scopes} == EXPORT[who]


@pytest.mark.parametrize("who", sorted(PRINCIPALS))
async def test_erasure_status_matrix(world: Any, who: str, monkeypatch: pytest.MonkeyPatch) -> None:
    set_current_auth(PRINCIPALS[who])
    assert {s: await _status(s, monkeypatch) for s in SCOPES} == STATUS[who]


@pytest.mark.parametrize("who", sorted(PRINCIPALS))
async def test_erasure_list_matrix(world: Any, who: str) -> None:
    set_current_auth(PRINCIPALS[who])
    assert await _list(world) == LIST[who]


async def test_no_auth_context_passes_every_handler(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth disabled (no principal): the handlers add no check of their own."""
    set_current_auth(None)
    assert {s: await _request(s) for s in SCOPES} == dict.fromkeys(SCOPES, "ok")
    assert {s: await _export(s) for s in ("ns_own", "user_id")} == {"ns_own": "ok", "user_id": "ok"}
    assert await _status("user_id", monkeypatch) == "ok"
    assert await _list(world) == "org"
