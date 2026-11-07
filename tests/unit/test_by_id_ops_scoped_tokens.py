"""An op addressed by an org-wide id admits a scoped token and
authorizes it on the row's own namespace.

`memory.get_by_id`, `memory.update_by_id`, `memory.redact`, `erasure.status`
and `blacklist.remove` carry no `namespace` field: the id names the row, and
the handler finds the row's namespace and checks it (`row_in_scope`). The
transport's gate must not answer a scoped (ns_roles) token
IDENTITY_INCOMPLETE before the handler runs — these are the ops the MCP core
roster hands a conflict-resolution flow (`loom_memory_update_by_id`). A
scoped token must reach the handler, and the
handler's check must stay the only thing that decides: a token scoped to
namespace A never reads, updates or redacts a row in namespace B, and a
foreign row answers exactly like an unknown id."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.authz import check_authorized
from server.auth.jwt import AuthInfo, mint_token
from server.core.errors import IdentityIncompleteError
from server.memory import store as memory_repo
from server.memory.app import update as update_mod
from server.memory.store import MemoryRow
from server.memory.store.history import HistoryTarget
from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings

_OWN = "11111111-1111-1111-1111-111111111111"
_FOREIGN = "22222222-2222-2222-2222-222222222222"
_ROW_SCOPED = [
    "blacklist.remove",
    "erasure.status",
    "memory.get_by_id",
    "memory.redact",
    "memory.update_by_id",
]


def _op(name: str):  # type: ignore[no-untyped-def]
    return next(op for op in ALL_OPERATIONS if op.name == name)


def _row(memory_id: str, namespace: str) -> MemoryRow:
    return MemoryRow(
        memory_id=memory_id,
        org="dev",
        namespace=namespace,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=7,
        pinned=False,
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
        expires_at=None,
    )


_ROWS = {_OWN: _row(_OWN, "alerts-bot"), _FOREIGN: _row(_FOREIGN, "someone-else")}


async def _fake_get_by_id(memory_id: str, *, namespace: str, **_: Any) -> MemoryRow | None:
    row = _ROWS.get(memory_id)
    return row if row is not None and row.namespace == namespace else None


async def _fake_namespaces_of_id(_org: str, memory_id: str, **_: Any) -> list[str]:
    return [_ROWS[memory_id].namespace] if memory_id in _ROWS else []


def _token(role: str = "writer") -> str:
    return mint_token(
        subject="sa:alerts-bot",
        org="dev",
        namespaces=["alerts-bot"],
        roles=[],
        ns_roles={"alerts-bot": role},
        ttl_seconds=300,
        is_service_account=True,
    )


@pytest.fixture()
def http(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server.transport import http as http_mod
    from server.transport.http import build_router

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(memory_repo, "get_by_id", _fake_get_by_id)
    monkeypatch.setattr(memory_repo, "namespaces_of_id", _fake_namespaces_of_id)
    app = FastAPI()
    app.include_router(build_router())
    return TestClient(app)


def _post(c: TestClient, op: str, body: dict[str, Any], role: str = "writer"):  # type: ignore[no-untyped-def]
    return c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {_token(role)}"})


# ---- the flag and the gate ----


def test_exactly_the_id_addressed_ops_are_row_scoped() -> None:
    assert sorted(o.name for o in ALL_OPERATIONS if o.row_scoped) == _ROW_SCOPED
    for name in _ROW_SCOPED:
        op = _op(name)
        assert "namespace" not in op.input_model.model_fields, name
        assert not op.deployment_scoped, name


def test_every_row_scoped_handler_authorizes_the_rows_namespace() -> None:
    """The flag moves the namespace check from the transport into the
    handler; a handler that forgot it would serve any namespace's row.
    `namespace_for_id` keeps only the namespaces `row_in_scope` admits."""
    gates = ("row_in_scope", "_authorize_status_read", "namespace_for_id")
    for name in _ROW_SCOPED:
        src = inspect.getsource(_op(name).handler)
        assert any(g in src for g in gates), name


def _scoped(role: str = "viewer", org: str = "dev") -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org=org,
        namespaces=["a"],
        roles=(),
        is_service_account=True,
        ns_roles=(("a", role),),
    )


def test_the_gate_admits_a_scoped_token_holding_the_role_somewhere() -> None:
    asyncio.run(check_authorized(_scoped("writer"), Role.WRITER, org="dev", row_scoped=True))
    asyncio.run(check_authorized(_scoped("viewer"), Role.VIEWER, org=None, row_scoped=True))
    # Short everywhere → refused at the gate; another org → refused at the gate.
    with pytest.raises(PermissionError):
        asyncio.run(check_authorized(_scoped("viewer"), Role.WRITER, org="dev", row_scoped=True))
    with pytest.raises(PermissionError):
        asyncio.run(
            check_authorized(_scoped("admin", org="other"), Role.VIEWER, org="dev", row_scoped=True)
        )
    # Without the flag the namespace-less call is still incomplete.
    with pytest.raises(IdentityIncompleteError):
        asyncio.run(check_authorized(_scoped("writer"), Role.WRITER, org="dev"))


# ---- memory.get_by_id ----


def test_scoped_token_reads_its_own_row_by_id(http: TestClient) -> None:
    r = _post(http, "memory.get_by_id", {"org": "dev", "memory_id": _OWN}, role="viewer")
    assert r.status_code == 200, r.text
    assert r.json()["found"] is True and r.json()["namespace"] == "alerts-bot"


def test_a_foreign_row_answers_like_an_unknown_id(http: TestClient) -> None:
    foreign = _post(http, "memory.get_by_id", {"org": "dev", "memory_id": _FOREIGN})
    unknown = _post(
        http,
        "memory.get_by_id",
        {"org": "dev", "memory_id": "33333333-3333-3333-3333-333333333333"},
    )
    assert foreign.status_code == unknown.status_code == 200
    assert foreign.json() == unknown.json() == {**foreign.json(), "found": False}
    assert "someone-else" not in foreign.text


# ---- memory.update_by_id ----


@pytest.fixture()
def updates(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from server.memory.api.models_crud import MemoryUpdateByIdOutput
    from server.memory.app import handlers as memory_handlers
    from server.memory.app import restore as _restore_mod
    from server.memory.app import stack as _stack_mod

    seen: list[str] = []

    async def _write(input: Any, existing: MemoryRow, ch: Any, *, op: str) -> Any:
        seen.append(existing.namespace)
        return MemoryUpdateByIdOutput(updated=True, memory_id=existing.memory_id)

    monkeypatch.setattr(update_mod, "_update_live_row", _write)
    monkeypatch.setattr(memory_handlers, "get_client", lambda: None)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: None)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: None)
    monkeypatch.setattr("server.memory.app.write.get_client", memory_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", memory_handlers.get_client)
    return seen


def test_scoped_writer_updates_its_own_row_by_id(http: TestClient, updates: list[str]) -> None:
    r = _post(http, "memory.update_by_id", {"org": "dev", "memory_id": _OWN, "value": "new"})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] is True and updates == ["alerts-bot"]


def test_scoped_writer_never_updates_a_foreign_row(http: TestClient, updates: list[str]) -> None:
    r = _post(http, "memory.update_by_id", {"org": "dev", "memory_id": _FOREIGN, "value": "x"})
    assert r.status_code == 200 and r.json()["updated"] is False
    assert updates == []


def test_scoped_viewer_is_refused_before_the_lookup(http: TestClient, updates: list[str]) -> None:
    r = _post(
        http, "memory.update_by_id", {"org": "dev", "memory_id": _OWN, "value": "x"}, role="viewer"
    )
    assert r.status_code == 403, r.text
    assert updates == []


def test_scoped_writer_updates_by_id_over_mcp(
    monkeypatch: pytest.MonkeyPatch, updates: list[str]
) -> None:
    """The MCP core roster's conflict-resolution verb, from the Claude Code
    plugin's scoped bearer."""
    import server.transport.mcp_call as mcp_call
    from server.auth.jwt import verify_token

    monkeypatch.setattr(memory_repo, "get_by_id", _fake_get_by_id)
    monkeypatch.setattr(memory_repo, "namespaces_of_id", _fake_namespaces_of_id)

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "dev", "namespace": "alerts-bot", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    auth = verify_token(_token())
    for mid in (_OWN, _FOREIGN):
        resp = asyncio.run(
            mcp_call._call_tool(
                1,
                {
                    "name": "loom_memory_update_by_id",
                    "arguments": {"memory_id": mid, "value": "new"},
                },
                auth=auth,
            )
        )
        assert not resp["result"].get("isError"), resp
    assert updates == ["alerts-bot"]


# ---- memory.redact ----


@pytest.fixture()
def redact_targets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from server.memory.app import curation as _memory_curation

    reached: list[str] = []
    targets = {
        _OWN: {
            "namespace": "alerts-bot",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k",
            "version": 7,
            "tombstoned_at": None,
        },
        _FOREIGN: {
            "namespace": "someone-else",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k",
            "version": 7,
            "tombstoned_at": None,
        },
    }

    async def _find(org: str, memory_id: str, _version: int, **_: Any) -> HistoryTarget | None:
        row = targets.get(memory_id)
        if row is None:
            return None
        return HistoryTarget.from_row({**row, "org": org, "memory_id": memory_id})

    async def _head(identity: Any, key: str, **_: Any) -> MemoryRow:
        reached.append(identity.namespace)
        return _row(_OWN, identity.namespace)  # version 7 == the target → refused_head

    monkeypatch.setattr(memory_repo, "find_history_target", _find)
    monkeypatch.setattr(memory_repo, "get_by_key", _head)
    assert _memory_curation is not None
    return reached


def test_scoped_admin_reaches_redact_only_on_its_own_row(
    http: TestClient, redact_targets: list[str]
) -> None:
    body = {"org": "dev", "version": 7, "reason": "leak"}
    own = _post(http, "memory.redact", {**body, "memory_id": _OWN}, role="admin")
    foreign = _post(http, "memory.redact", {**body, "memory_id": _FOREIGN}, role="admin")
    assert own.status_code == 200, own.text
    assert own.json()["refused_head"] is True  # reached the head check of its own row
    assert foreign.status_code == 200 and foreign.json()["redacted"] is False
    assert not foreign.json().get("refused_head")
    assert redact_targets == ["alerts-bot"]


def test_scoped_writer_cannot_redact(http: TestClient, redact_targets: list[str]) -> None:
    r = _post(
        http, "memory.redact", {"org": "dev", "memory_id": _OWN, "version": 7, "reason": "leak"}
    )
    assert r.status_code == 403, r.text
    assert redact_targets == []


# ---- erasure.status ----


@pytest.fixture()
def sagas(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.erasure.app import handlers as erasure_handlers

    base = {
        "org": "dev",
        "requester": "r",
        "status": "completed",
        "current_step": 3,
        "requested_at": datetime(2026, 9, 24),
        "completed_at": None,
        "failure_reason": "",
        "residue_count": 0,
        "residue": "",
        "verified_at": None,
    }
    rows = {
        "own": {**base, "id": "own", "scope_type": "namespace", "scope_value": "alerts-bot"},
        "foreign": {
            **base,
            "id": "foreign",
            "scope_type": "namespace",
            "scope_value": "someone-else",
        },
        "user": {**base, "id": "user", "scope_type": "user_id", "scope_value": "u-1"},
    }

    async def _fetch(erasure_id: str) -> dict[str, Any] | None:
        return rows.get(erasure_id)

    monkeypatch.setattr(erasure_handlers.erasure_ledger, "fetch_request_status", _fetch)


def test_scoped_token_reads_its_own_namespaces_erasure(http: TestClient, sagas: None) -> None:
    r = _post(http, "erasure.status", {"erasure_id": "own"}, role="viewer")
    assert r.status_code == 200, r.text
    assert r.json()["found"] is True
    # Another namespace's saga answers exactly like an unknown id: a 403 would
    # confirm the id exists.
    foreign = _post(http, "erasure.status", {"erasure_id": "foreign"})
    unknown = _post(http, "erasure.status", {"erasure_id": "no-such-saga"})
    assert foreign.status_code == unknown.status_code == 200, foreign.text
    assert foreign.json()["found"] is False and unknown.json()["found"] is False
    assert "someone-else" not in foreign.text


def test_a_user_wide_erasure_is_not_read_through_a_scoped_token(
    http: TestClient, sagas: None
) -> None:
    """A user_id saga spans every namespace of the org; a token scoped to one
    namespace does not see it (org membership is a flat token's gate); it
    answers found=False, like an unknown id."""
    r = _post(http, "erasure.status", {"erasure_id": "user"}, role="admin")
    assert r.status_code == 200, r.text
    assert r.json()["found"] is False
    assert "u-1" not in r.text


# ---- flat tokens keep the plain path ----


def test_a_flat_token_is_unaffected_by_the_flag() -> None:
    flat = AuthInfo(
        subject="u", org="dev", namespaces=[], roles=(Role.WRITER,), is_service_account=False
    )
    asyncio.run(check_authorized(flat, Role.WRITER, org="dev", row_scoped=True))
    asyncio.run(check_authorized(flat, Role.WRITER, org="dev"))
    other_org = AuthInfo(
        subject="u", org="other", namespaces=[], roles=(Role.WRITER,), is_service_account=False
    )
    for flag in (True, False):
        with pytest.raises(PermissionError):
            asyncio.run(check_authorized(other_org, Role.WRITER, org="dev", row_scoped=flag))


def test_op_dataclass_carries_the_flag() -> None:
    assert "row_scoped" in {f.name for f in dataclasses.fields(_op("memory.get_by_id"))}
