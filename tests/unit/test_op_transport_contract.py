"""Every registered operation, on both transports, admits a call only through
its gates.

The matrix is generated from `ALL_OPERATIONS`, so an operation added later
is covered without anyone listing it:

- HTTP: no bearer, a malformed Authorization header or a rejected token is a
  401 AUTH_INVALID; a body that is not JSON is a 400 and a JSON body that is
  not an object is a 422 VALIDATION_FAILED;
- HTTP and MCP: a wrong-typed `namespace` is refused naming that field
  (HTTP 422 VALIDATION_FAILED; MCP isError with the field in `errors`);
- HTTP and MCP: a caller whose role is one rank below the op's
  `required_role` is refused AUTH_INSUFFICIENT;
- MCP: an op kept off MCP is OP_NOT_ON_MCP for every caller, the op's own
  role included;
- HTTP and MCP: an org admin of another org is refused, except by the ops
  that authorize the org inside the handler (`_ORG_CHECKED_IN_HANDLER`,
  each with the test that covers its refusal);
- no refusal runs the handler, and a caller at the op's role reaches it over
  HTTP, and over MCP when the op is on MCP (the positive control that proves
  the refusals come from the gates, not from a broken probe).

The real role gate (`server.auth.authz.check_authorized`) runs. Only its
storage reads are stubbed: no namespace is restricted, org-readable or
granted, and the member-write rule admits nobody. The limiter is off (one
namespace takes a call per op per test, and a bucket is not under test here).
The handlers are stubs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
import types
import typing
from collections.abc import Iterator
from typing import Any

import annotated_types
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import server.transport.mcp_call as mcp_call
import server.transport.mcp_outcome as mcp_outcome
from server.auth.jwt import AuthInfo, TokenError
from server.operations.base import Operation, Role
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings
from server.transport.mcp_config import _LOOM_ACTIVATE_TOOL_NAME, _LOOM_SEARCH_TOOL_NAME
from server.transport.mcp_op import _MCP_HIDDEN_OPS

ORG = "acme"
NAMESPACE = "team"
_HEX = "abcdef0123456789"  # a commit sha, a batch id and a plain string at once

# Inputs whose model validators need a specific combination the field
# annotations cannot express.
_BODIES: dict[str, dict[str, Any]] = {
    "memory.touch": {"memory_key": "k", "clear_expiry": True},
    "memory.apply": {"target": {"keys": ["k"]}},
    "run.grade": {"run_id": "r", "grade": "pass"},
    "code.outcome": {"action": "opened_file", "trace_run_id": "r"},
    "code.scip_ingest": {"batch_id": "00000000-0000-4000-8000-000000000001"},
    "knowledge.synthesize": {"source_uris": ["doc://a"]},
}


def _min_len(metadata: tuple[Any, ...]) -> int:
    for m in metadata:
        if isinstance(m, annotated_types.MinLen):
            return int(m.min_length)
        n = getattr(m, "min_length", None)
        if n:
            return int(n)
    return 0


def _value(ann: Any, metadata: tuple[Any, ...] = ()) -> Any:
    """A value `ann` accepts: the first member of a Literal/Enum, the shortest
    list its min_length allows, a nested model's own required fields."""
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if origin is typing.Annotated:
        return _value(args[0], metadata + tuple(args[1:]))
    if origin in (typing.Union, types.UnionType):
        return _value(next(a for a in args if a is not type(None)), metadata)
    if origin is typing.Literal:
        return args[0]
    if origin in (list, tuple, set, frozenset):
        return [_value(args[0] if args else str) for _ in range(_min_len(metadata))]
    if origin is dict:
        return {}
    if isinstance(ann, type):
        if issubclass(ann, bool):
            return False
        if issubclass(ann, enum.Enum):
            return next(iter(ann)).value
        if issubclass(ann, int):
            return 1
        if issubclass(ann, float):
            return 0.5
        if issubclass(ann, BaseModel):
            return _required(ann)
        if issubclass(ann, datetime.datetime):
            return "2020-01-01T00:00:00"
        if issubclass(ann, datetime.date):
            return "2020-01-01"
    return _HEX


def _required(model: type[BaseModel]) -> dict[str, Any]:
    return {
        f.alias or name: _value(f.annotation, tuple(f.metadata))
        for name, f in model.model_fields.items()
        if f.is_required()
    }


def _body(op: Operation) -> dict[str, Any]:
    body = {**_required(op.input_model), **_BODIES.get(op.name, {})}
    fields = op.input_model.model_fields
    for name, value in (("org", ORG), ("namespace", NAMESPACE), ("agent", "probe")):
        if name in fields:
            body[name] = value
    return body


def _below(role: Role) -> Role:
    return max(r for r in Role if r < role)


def _principal(role: Role) -> AuthInfo:
    return AuthInfo(
        subject=f"probe-{role.name.lower()}",
        org=ORG,
        namespaces=("*",),
        roles=(role,),
        is_service_account=True,
    )


_RIVAL = dataclasses.replace(_principal(Role.ORG_ADMIN), subject="rival-admin", org="rival")
_TOKENS = {f"tok-{r.name.lower()}": _principal(r) for r in Role} | {"tok-rival": _RIVAL}

# Ops whose input names no `org` for the transport to check (an org-wide id,
# or a `from_org`), so the handler is the org boundary, and the test that
# holds it there.
_ORG_CHECKED_IN_HANDLER = {
    "erasure.status": "test_transport_security::test_erasure_status_foreign_org_token_is_not_found",
    "memory.cross_promote": "test_cross_promote_source_scope::test_another_orgs_admin_cannot_cross_promote",
    "memory.copy_to": "test_cross_promote_source_scope::test_another_orgs_admin_cannot_copy_into_or_out_of_an_org",
}


def _token(role: Role) -> str:
    return f"tok-{role.name.lower()}"


@dataclasses.dataclass
class _Env:
    http: TestClient
    ran: list[str]


@pytest.fixture(scope="module")
def _app() -> Iterator[_Env]:
    """One router over every op for the module: building it is most of the cost."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield from _patched_app(monkeypatch)


def _patched_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    import server.auth.authz as authz
    import server.auth.bearer as bearer_mod
    import server.code.app.member_writes as member_writes
    from server.transport import access_refusal, gates
    from server.transport import http as http_mod

    ran: list[str] = []

    def _stub(op: Operation) -> Operation:
        async def _handler(_inp: Any) -> BaseModel:
            ran.append(op.name)
            return op.output_model.model_construct()

        return dataclasses.replace(op, handler=_handler)

    ops = [_stub(op) for op in ALL_OPERATIONS]

    def _verify(token: str) -> AuthInfo:
        if token not in _TOKENS:
            raise TokenError("bad token")
        return _TOKENS[token]

    async def _no(*_a: Any, **_k: Any) -> bool:
        return False

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(authz, "may_be_restricted", _no)
    monkeypatch.setattr(authz, "is_org_readable_code_namespace", _no)
    monkeypatch.setattr(authz, "_lookup_grant", _none)
    monkeypatch.setattr(member_writes, "code_member_may_write", _no)
    monkeypatch.setattr("server.transport.gates._admitted", _none)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _none)
    monkeypatch.setattr(access_refusal, "is_plane_op", lambda name: False)
    monkeypatch.setattr(gates, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "code_org", ORG)
    monkeypatch.setattr(settings, "code_namespace", NAMESPACE)
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    for op in ops:
        monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, op.mcp_tool_name, op)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _none)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield _Env(http=client, ran=ran)


@pytest.fixture
def env(_app: _Env) -> Iterator[_Env]:
    _app.ran.clear()
    yield _app
    _app.ran.clear()


def _post(env: _Env, op: Operation, body: Any, auth: str | None, **kw: Any) -> Any:
    headers = {"Authorization": auth} if auth is not None else {}
    return env.http.post(op.http_path, json=body, headers=headers, **kw)


def _code(response: Any) -> str:
    detail = response.json().get("detail")
    return detail.get("code", "") if isinstance(detail, dict) else ""


def _mcp(op: Operation, body: dict[str, Any], role: Role | AuthInfo) -> dict[str, Any]:
    params = {"name": op.mcp_tool_name, "arguments": dict(body)}
    auth = role if isinstance(role, AuthInfo) else _principal(role)
    return asyncio.run(mcp_call._call_tool(1, params, auth=auth))


def _report(failures: list[str]) -> None:
    assert not failures, f"{len(failures)} op(s):\n" + "\n".join(failures)


def test_every_op_has_a_body_its_input_model_accepts() -> None:
    """The matrix's own precondition: a refusal below is the gate's, not the
    validator's. An op whose validators need a specific combination of
    fields gets an entry in `_BODIES`."""
    failures = []
    for op in ALL_OPERATIONS:
        try:
            op.input_model.model_validate(_body(op))
        except Exception as e:  # noqa: BLE001 — every failure is reported, not the first
            failures.append(f"{op.name}: {str(e).splitlines()[:2]}")
    _report(failures)
    assert not set(_BODIES) - {op.name for op in ALL_OPERATIONS}, "stale _BODIES entry"


@pytest.mark.parametrize(
    ("auth", "why"),
    [
        (None, "no Authorization header"),
        ("Basic dXNlcjpwdw==", "not a bearer"),
        ("Bearer forged", "rejected token"),
    ],
)
def test_every_op_401s_an_unauthenticated_http_call(env: _Env, auth: str | None, why: str) -> None:
    failures = []
    for op in ALL_OPERATIONS:
        r = _post(env, op, _body(op), auth)
        if (r.status_code, _code(r)) != (401, "AUTH_INVALID"):
            failures.append(f"{op.name}: {r.status_code} {_code(r)} ({why})")
    _report(failures)
    assert env.ran == []


def test_every_op_refuses_a_body_that_is_not_a_json_object(env: _Env) -> None:
    token = f"Bearer {_token(Role.PLATFORM_ADMIN)}"
    failures = []
    for op in ALL_OPERATIONS:
        r = _post(env, op, None, token, content=b"{not json")
        if (r.status_code, _code(r)) != (400, "VALIDATION_FAILED"):
            failures.append(f"{op.name}: invalid JSON gave {r.status_code} {_code(r)}")
        r = _post(env, op, ["an", "array"], token)
        if (r.status_code, _code(r)) != (422, "VALIDATION_FAILED"):
            failures.append(f"{op.name}: a JSON array gave {r.status_code} {_code(r)}")
    _report(failures)
    assert env.ran == []


def test_every_op_refuses_a_wrong_typed_field_by_name_on_both_transports(env: _Env) -> None:
    token = f"Bearer {_token(Role.PLATFORM_ADMIN)}"
    failures = []
    for op in ALL_OPERATIONS:
        if "namespace" not in op.input_model.model_fields:
            continue
        body = {**_body(op), "namespace": ["not", "a", "string"]}
        r = _post(env, op, body, token)
        locs = [e.get("loc") for e in (r.json().get("detail") or {}).get("errors", [])]
        if (r.status_code, _code(r)) != (422, "VALIDATION_FAILED") or ["namespace"] not in locs:
            failures.append(f"{op.name} http: {r.status_code} {_code(r)} {locs}")
        if op in _ON_MCP:
            out = _mcp(op, body, Role.PLATFORM_ADMIN).get("result") or {}
            errors = (out.get("structuredContent") or {}).get("errors") or []
            if not (out.get("isError") and ["namespace"] in [e.get("loc") for e in errors]):
                failures.append(f"{op.name} mcp: isError={out.get('isError')} {errors}")
    _report(failures)
    assert env.ran == []


_GATED = [op for op in ALL_OPERATIONS if op.required_role > Role.VIEWER]
# An op whose MCP tool name is a hand-written tool (`search` is `loom_search`)
# is answered on MCP by that tool's own path, which its own tests cover.
_SYNTHETIC_TOOLS = {_LOOM_SEARCH_TOOL_NAME, _LOOM_ACTIVATE_TOOL_NAME}
_ON_MCP = [
    op
    for op in ALL_OPERATIONS
    if op.name not in _MCP_HIDDEN_OPS and op.mcp_tool_name not in _SYNTHETIC_TOOLS
]


def test_the_role_matrix_has_ops_at_every_gated_role() -> None:
    assert {op.required_role for op in _GATED} >= {Role.WRITER, Role.ADMIN, Role.ORG_ADMIN}


def test_every_gated_op_refuses_the_role_below_its_floor_over_http(env: _Env) -> None:
    failures = []
    for op in _GATED:
        below = _below(op.required_role)
        r = _post(env, op, _body(op), f"Bearer {_token(below)}")
        if (r.status_code, _code(r)) != (403, "AUTH_INSUFFICIENT"):
            failures.append(f"{op.name} as {below.name}: {r.status_code} {_code(r)}")
    _report(failures)
    assert env.ran == []


def test_every_gated_op_refuses_the_role_below_its_floor_over_mcp(env: _Env) -> None:
    failures = []
    for op in (op for op in _GATED if op in _ON_MCP):
        below = _below(op.required_role)
        out = _mcp(op, _body(op), below).get("result") or {}
        code = (out.get("structuredContent") or {}).get("code")
        if not (out.get("isError") and code == "AUTH_INSUFFICIENT"):
            failures.append(f"{op.name} as {below.name}: isError={out.get('isError')} {code}")
    _report(failures)
    assert env.ran == []


def test_an_op_kept_off_mcp_is_refused_there_for_every_role(env: _Env) -> None:
    hidden = [op for op in ALL_OPERATIONS if op.name in _MCP_HIDDEN_OPS]
    assert hidden and not set(_MCP_HIDDEN_OPS) - {op.name for op in hidden}, "stale hidden op"
    failures = []
    for op in hidden:
        for role in {op.required_role, Role.PLATFORM_ADMIN}:
            out = _mcp(op, _body(op), role).get("result") or {}
            code = (out.get("structuredContent") or {}).get("code")
            if not (out.get("isError") and code == "OP_NOT_ON_MCP"):
                failures.append(f"{op.name} as {role.name}: isError={out.get('isError')} {code}")
    _report(failures)
    assert env.ran == []


def test_another_orgs_admin_reaches_no_handler_the_transport_can_check(env: _Env) -> None:
    admitted = set()
    for op in ALL_OPERATIONS:
        before = len(env.ran)
        _post(env, op, _body(op), "Bearer tok-rival")
        if op in _ON_MCP:
            _mcp(op, _body(op), _RIVAL)
        if len(env.ran) > before:
            admitted.add(op.name)
    assert admitted == set(_ORG_CHECKED_IN_HANDLER)


def test_each_handler_org_check_names_a_test_that_exists() -> None:
    from pathlib import Path

    for op, ref in _ORG_CHECKED_IN_HANDLER.items():
        module, name = ref.split("::")
        source = (Path(__file__).parent / f"{module}.py").read_text(encoding="utf-8")
        assert f"def {name}(" in source, f"{op}: {ref} is gone"


def test_a_caller_at_the_ops_role_reaches_its_handler(env: _Env) -> None:
    """Positive control for the refusals above."""
    missed = []
    for op in ALL_OPERATIONS:
        before = len(env.ran)
        _post(env, op, _body(op), f"Bearer {_token(op.required_role)}")
        want = 2 if op in _ON_MCP else 1
        if op in _ON_MCP:
            _mcp(op, _body(op), op.required_role)
        if env.ran[before:] != [op.name] * want:
            missed.append(f"{op.name}: handler ran {env.ran[before:].count(op.name)}/{want}")
    _report(missed)
