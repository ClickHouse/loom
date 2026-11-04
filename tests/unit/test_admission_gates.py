"""HTTP and MCP admit a call through one sequence (server/transport/gates.py).

authn → authorization target → authorization → policy → rate limit →
dispatch, on both transports:

- the target treats an empty `org` / `namespace` as absent, defaults a
  code.* / tracker.* call to its plane and otherwise to the token's org, and
  refuses (IDENTITY_INCOMPLETE) a namespace it cannot pair with an org;
- both transports authorize the same (org, namespace) for the same request;
- a refused caller never draws the limiter (another tenant cannot spend a
  namespace's bucket by naming it);
- a role refusal is AUTH_INSUFFICIENT on both, in the answer and the ledger;
- a limiter or classifier that raises admits the call and is counted and
  logged once per interval.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import server.auth.bearer as bearer_mod
import server.transport.mcp_call as mcp_call
import server.transport.mcp_outcome as mcp_outcome
from server.auth.jwt import AuthInfo, TokenError
from server.core import fail_open
from server.core.errors import IdentityIncompleteError
from server.operations.base import Operation, Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.settings.catalog import OP_BUDGETS_P99_MS
from server.transport import gates


def _principal(subject: str, org: str = "o", ns: tuple[str, ...] = ("n",)) -> AuthInfo:
    return AuthInfo(
        subject=subject, org=org, namespaces=ns, roles=(Role.WRITER,), is_service_account=True
    )


# ------------------------------------------------------------------ target


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-acme")
    monkeypatch.setattr(settings, "tracker_org", "")


def test_an_empty_org_or_namespace_is_absent_and_takes_the_plane(plane: None) -> None:
    op = OPERATIONS_BY_NAME["code.search"]
    for org, ns in (("", ""), (None, None), ("-", "-")):
        t = gates.authz_target(op, org=org, namespace=ns, auth=_principal("a", org="acme"))
        assert (t.org, t.namespace, t.named_namespace) == ("acme", "code-acme", None)


def test_the_token_org_fills_an_org_the_request_and_plane_leave_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_org", "")
    monkeypatch.setattr(settings, "code_namespace", "")
    op = OPERATIONS_BY_NAME["code.search"]
    t = gates.authz_target(op, org="", namespace="code-x", auth=_principal("a", org="o"))
    assert (t.org, t.namespace, t.named_namespace) == ("o", "code-x", "code-x")


def test_a_namespace_without_a_resolvable_org_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_org", "")
    op = OPERATIONS_BY_NAME["code.search"]
    with pytest.raises(IdentityIncompleteError):
        gates.authz_target(op, org="", namespace="code-x", auth=_principal("a", org="*"))
    # auth off authorizes nothing, so there is nothing to refuse
    assert gates.authz_target(op, org="", namespace="code-x", auth=None).org is None


def test_a_self_service_op_names_no_authz_namespace_but_registers_one() -> None:
    op = OPERATIONS_BY_NAME["namespace.join"]
    t = gates.authz_target(op, org="o", namespace="team", auth=_principal("a"))
    assert (t.org, t.namespace, t.register_namespace) == ("o", None, "team")


# ------------------------------------------------------------------ parity


class _In(BaseModel):
    org: str = ""
    namespace: str = ""
    agent: str = "-"


class _Out(BaseModel):
    ok: bool = True


TOOL = "loom_test_gate_probe"


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch, plane: None) -> dict[str, Any]:
    """One op on both transports; the role gate records its target and
    refuses subject "intruder"; a real limiter with a one-token bucket."""
    import server.code.app.member_writes as member_writes
    import server.core.ratelimit as ratelimit_mod
    from server.core.ratelimit import Limit, Limiter
    from server.transport import http as http_mod

    seen: dict[str, Any] = {"authz": [], "ran": 0, "ledger": []}

    async def _handler(inp: Any) -> _Out:
        seen["ran"] += 1
        return _Out()

    op = Operation(
        name="code.test_gate_probe",
        input_model=_In,
        output_model=_Out,
        handler=_handler,
        idempotent=False,
        required_role=Role.VIEWER,
    )

    async def _authorize_op(auth: Any, op_: Any, *, org: Any, namespace: Any, **_k: Any) -> None:
        seen["authz"].append((org, namespace))
        if auth.subject == "intruder":
            raise PermissionError("not a member")

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    limiter = Limiter()
    limiter.set_namespace_limit("team", "code", Limit(capacity=1, rate=1e-6))
    seen["limiter"] = limiter
    tokens = {"alice": _principal("alice", org="acme"), "intruder": _principal("intruder", org="p")}

    def _verify(token: str) -> AuthInfo:
        if token not in tokens:
            raise TokenError("bad token")
        return tokens[token]

    monkeypatch.setattr(member_writes, "authorize_op", _authorize_op)
    monkeypatch.setattr("server.transport.gates._admitted", _noop)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _noop)
    monkeypatch.setattr(ratelimit_mod, "get_limiter", lambda: limiter)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    # HTTP
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setitem(OP_BUDGETS_P99_MS, op.name, 10_000)  # a probe op's budget
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    monkeypatch.setattr(
        http_mod, "_emit_event", lambda *a, **k: seen["ledger"].append(("http", k["error_code"]))
    )
    app = FastAPI()
    app.include_router(http_mod.build_router())
    seen["http"] = TestClient(app)
    seen["tokens"] = tokens
    # MCP
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, TOOL, op)

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    for mod in (mcp_call, mcp_outcome):
        monkeypatch.setattr(
            mod,
            "_emit_mcp_event",
            lambda *a, **k: seen["ledger"].append(("mcp", k.get("error_code", ""))),
        )
    return seen


def _http(seen: dict[str, Any], who: str, body: dict[str, Any]) -> Any:
    return seen["http"].post(
        "/v1/code.test_gate_probe", json=body, headers={"Authorization": f"Bearer {who}"}
    )


def _mcp(seen: dict[str, Any], who: str, body: dict[str, Any]) -> dict[str, Any]:
    params = {"name": TOOL, "arguments": dict(body)}
    return asyncio.run(mcp_call._call_tool(1, params, auth=seen["tokens"][who]))


@pytest.mark.parametrize(
    ("body", "target"),
    [
        ({"org": "", "namespace": ""}, ("acme", "code-acme")),
        ({}, ("acme", "code-acme")),
        ({"org": "acme", "namespace": "team"}, ("acme", "team")),
    ],
)
def test_both_transports_authorize_the_same_target(
    probe: dict[str, Any], body: dict[str, Any], target: tuple[str, str]
) -> None:
    probe["limiter"].clear_namespace_limit("team", "code")
    assert _http(probe, "alice", body).status_code == 200
    _mcp(probe, "alice", body)
    assert probe["authz"] == [target, target]
    assert probe["ran"] == 2


def test_both_transports_hand_the_policy_gate_one_identity(
    probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.identity.app.policy_gate as policy_gate

    seen: list[dict[str, str]] = []

    async def _gate(op: Any, identity: dict[str, str], args: Any, *, request_id: str) -> None:
        seen.append(dict(identity))

    monkeypatch.setattr(policy_gate, "gate", _gate)
    probe["limiter"].clear_namespace_limit("team", "code")
    body = {"org": "acme", "namespace": "team", "agent": "bot"}
    assert _http(probe, "alice", body).status_code == 200
    _mcp(probe, "alice", body)
    want = {"org": "acme", "namespace": "team", "agent": "bot", "user_id": "-", "session_id": "-"}
    assert seen == [want, want]


def test_a_refused_caller_never_draws_the_bucket_on_either_transport(
    probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.transport import access_refusal

    # The probe is a code.* op for the plane defaults; a code index's refusal
    # adds its NO_REPO_ACCESS reason, which is not the point here.
    monkeypatch.setattr(access_refusal, "is_plane_op", lambda name: False)
    body = {"org": "acme", "namespace": "team"}
    for _ in range(3):
        r = _http(probe, "intruder", body)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
        out = _mcp(probe, "intruder", body)["result"]
        assert out["isError"] and out["structuredContent"]["code"] == "AUTH_INSUFFICIENT"
    assert probe["ledger"] == [("http", "AUTH_INSUFFICIENT"), ("mcp", "AUTH_INSUFFICIENT")] * 3
    # The namespace's one token is still there for its own caller.
    assert _http(probe, "alice", body).status_code == 200
    r = _http(probe, "alice", body)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "RATE_LIMITED"
    out = _mcp(probe, "alice", body)["result"]
    assert out["structuredContent"]["code"] == "RATE_LIMITED"
    assert probe["ran"] == 1


def test_a_limiter_that_raises_admits_the_call_and_logs_once(
    probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("bucket table unreadable")

    monkeypatch.setattr(probe["limiter"], "check", _boom)
    monkeypatch.setattr(fail_open, "_FAIL_OPEN_LAST_LOG", {})
    before = fail_open.counts().get("rate_limit", 0)
    body = {"org": "acme", "namespace": "team"}
    with caplog.at_level(logging.WARNING, logger="server.core.fail_open"):
        for _ in range(2):
            assert _http(probe, "alice", body).status_code == 200
            assert _mcp(probe, "alice", body)["result"]["isError"] is False
    assert probe["ran"] == 4
    assert fail_open.counts()["rate_limit"] == before + 4
    assert sum("rate_limit failed open" in r.getMessage() for r in caplog.records) == 1


def test_a_classifier_that_raises_is_interactive_on_both(
    probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.identity.app.traffic_class_registry as tcr

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("registry read failed")

    monkeypatch.setattr(tcr, "classify", _boom)
    probe["limiter"].clear_namespace_limit("team", "code")
    r = _http(probe, "alice", {"org": "acme", "namespace": "team"})
    assert r.status_code == 200 and r.headers["X-Loom-Traffic-Class"].startswith("interactive")
    assert _mcp(probe, "alice", {"org": "acme", "namespace": "team"})["result"]["isError"] is False


def test_the_transports_hold_no_copy_of_the_sequence() -> None:
    """Both call the shared gate; neither calls the role gate or the
    limiter itself."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in ("server/transport/http.py", "server/transport/mcp_call.py"):
        src = (root / rel).read_text()
        assert "gates.authz_target(" in src and "await gates.authorize(" in src, rel
        assert "gates.rate_limit(" in src, rel
        assert "await authorize_op(" not in src, rel
    assert dataclasses.is_dataclass(gates.Target)
