"""server/observability/sli.py and the telemetry an alert rule reads:
op and status classes, the auth refusal reasons, the health-problem series."""

from __future__ import annotations

from fastapi import HTTPException

from server.auth.jwt import AuthUnavailable, TokenError
from server.core.errors import BackendTimeoutError, RateLimitedError, ValidationError
from server.observability import sli
from server.observability.metrics import AUTH_REFUSAL_REASONS, auth_refusal_reason
from server.observability.metrics_gauges import health_problem_series
from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS
from server.transport._common import sli_columns


def test_every_registered_op_falls_in_a_known_class() -> None:
    for op in ALL_OPERATIONS:
        cls = sli.slo_class(
            op.name,
            writes=op.required_role == Role.WRITER,
            admin=op.required_role >= Role.ADMIN,
        )
        assert cls in sli.SLO_CLASSES, op.name


def test_slo_classes() -> None:
    assert sli.slo_class("code.search", writes=False, admin=False) == "code"
    assert sli.slo_class("memory.search", writes=False, admin=False) == "search"
    assert sli.slo_class("memory.set", writes=True, admin=False) == "write"
    assert sli.slo_class("memory.get", writes=False, admin=False) == "read"
    assert sli.slo_class("grants.approve", writes=False, admin=True) == "admin"


def test_status_class_comes_from_the_raised_status_then_the_code() -> None:
    assert sli.status_class(True, "", None) == "ok"
    assert sli.status_class(False, "RATE_LIMITED", RateLimitedError("slow down")) == "client"
    assert sli.status_class(False, "BACKEND_TIMEOUT", BackendTimeoutError("ch")) == "server"
    assert sli.status_class(False, "HANDLER_HTTP_ERROR", HTTPException(423, "locked")) == "client"
    assert sli.status_class(False, "HANDLER_HTTP_ERROR", HTTPException(502, "x")) == "server"
    # No exception: the code decides.
    assert sli.status_class(False, ValidationError.code, None) == "client"
    assert sli.status_class(False, "POLICY_FORBIDDEN", None) == "client"
    # A rule that denies is the caller's; a policy that could not be evaluated
    # (fail closed on a write) is the server's.
    from server.identity.app.policy_gate import PolicyForbiddenError
    from server.identity.domain.policy_rules import PolicyDecision

    denied = PolicyDecision("deny", ["r"], [], [], enforcing_rules=["r"])
    unread = PolicyDecision("deny", [], ["history read timed out"], [], enforcing_errors=["x"])
    assert sli.status_class(False, "POLICY_FORBIDDEN", PolicyForbiddenError("no", denied)) == (
        "client"
    )
    assert sli.status_class(False, "POLICY_FORBIDDEN", PolicyForbiddenError("no", unread)) == (
        "server"
    )
    assert sli.status_class(False, "HTTP_401", None) == "client"
    # An exception with no status (a bug, a timeout) is the server's.
    assert sli.status_class(False, "TimeoutError", TimeoutError()) == "server"
    assert sli.status_class(False, "INTERNAL_ERROR", RuntimeError("x")) == "server"


def test_sli_columns_resolve_the_effective_budget() -> None:
    cols = sli_columns("memory.get", "mcp")
    assert cols["transport"] == "mcp" and cols["slo_class"] == "read"
    assert cols["budget_ms"] > 0
    assert sli_columns("no.such.op", "http") == {"transport": "http"}


def test_auth_refusal_reasons_come_from_where_the_token_was_refused() -> None:
    cases = {
        None: "missing",
        AuthUnavailable("revocation list not loaded"): "unavailable",
        TokenError("bad header", reason="malformed"): "malformed",
        TokenError("token expired: Signature has expired", reason="expired"): "expired",
        TokenError("token jti 'x' is revoked", reason="revoked"): "revoked",
        TokenError("untrusted issuer 'revoked-iss'"): "invalid",
        TokenError("x", reason="not-a-reason"): "invalid",
    }
    for error, reason in cases.items():
        assert auth_refusal_reason(error) == reason
        assert reason in AUTH_REFUSAL_REASONS


def test_health_problem_series_splits_the_provider_out_of_the_code() -> None:
    series = health_problem_series(
        [
            {"code": "fake_llm", "severity": "warn"},
            {"code": "provider_failing:embedder", "severity": "error"},
        ]
    )
    assert series == [
        (1, {"code": "fake_llm", "severity": "warn"}),
        (1, {"code": "provider_failing", "severity": "error", "provider": "embedder"}),
    ]


def test_an_otlp_deployment_exports_its_metrics_to_the_same_endpoint() -> None:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    from server.observability.otel import _make_metric_exporter

    exporter = _make_metric_exporter("otlp", "http://collector:4318/")
    assert isinstance(exporter, OTLPMetricExporter)
    assert exporter._endpoint == "http://collector:4318/v1/metrics"


def test_every_code_the_transports_record_without_an_exception_is_classified() -> None:
    """A refusal written by code alone (no exception) must still be the
    caller's; only INTERNAL_ERROR is the server's."""
    import re

    from server.core.paths import REPO_ROOT

    literals = set()
    for path in (REPO_ROOT / "server" / "transport").glob("*.py"):
        literals |= set(re.findall(r'error_code="([A-Z_0-9]+)"', path.read_text(encoding="utf-8")))
    assert literals, "no literal codes found: the pattern no longer matches the transports"
    for code in sorted(literals):
        expected = "server" if code == "INTERNAL_ERROR" else "client"
        assert sli.status_class(False, code, None) == expected, code


class _Counter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def add(self, n: int, attributes: dict[str, str]) -> None:
        self.calls.append(attributes)


def test_a_refused_bearer_is_counted_once_on_each_transport(monkeypatch) -> None:
    import pytest

    from server.auth import bearer
    from server.observability import metrics
    from server.transport import mcp_auth

    counter = _Counter()
    monkeypatch.setattr(metrics, "_auth_refusals", counter)
    with pytest.raises(HTTPException) as e:
        bearer.verify_bearer_header("Basic abc")
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        bearer.verify_bearer("not-a-jwt")
    assert counter.calls == [
        {"reason": "malformed", "loom.transport": "http"},
        {"reason": "invalid", "loom.transport": "http"},
    ]

    counter.calls.clear()

    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}

    class _Req:
        headers: dict[str, str] = {}

    monkeypatch.setattr(mcp_auth, "_bearer_challenge", lambda *a, **k: "Bearer")
    mcp_auth._token_refused(_Req(), _Resp(), {"id": 1}, TokenError("expired", reason="expired"))
    mcp_auth._reject_unauthenticated(_Req(), _Resp(), {"id": 2}, message="missing bearer token")
    assert counter.calls == [
        {"reason": "expired", "loom.transport": "mcp"},
        {"reason": "missing", "loom.transport": "mcp"},
    ]


def test_a_missing_bearer_is_counted_on_every_http_route(monkeypatch) -> None:
    import asyncio

    import pytest
    from starlette.requests import Request

    from server.auth import routes
    from server.identity.app import me_callers, me_setup
    from server.insights.app import admin_routes
    from server.knowledge.app import download_routes
    from server.observability import metrics
    from server.operations.base import Role
    from server.tasks.app import watch_routes
    from server.transport import http

    counter = _Counter()
    monkeypatch.setattr(metrics, "_auth_refusals", counter)
    monkeypatch.setattr(type(http.settings), "auth_actually_required", lambda self: True)
    request = Request({"type": "http", "headers": []})
    refusals = (
        http._extract_auth,
        me_callers._authed,
        watch_routes._bearer_auth,
        lambda r: admin_routes._verified_admin(r, Role.ADMIN),
        lambda r: me_setup._setup_parent(r, {}),
        lambda r: asyncio.run(download_routes.knowledge_download(r, "o", "n", "s")),
    )
    for refuse in refusals:
        with pytest.raises(HTTPException) as e:
            refuse(request)
        assert e.value.status_code == 401
    assert counter.calls == [{"reason": "missing", "loom.transport": "http"}] * len(refusals)

    # The token check counts a refused token, not a signed-out caller.
    counter.calls.clear()
    assert asyncio.run(routes.whoami(request)) == {"authenticated": False}
    bad = Request({"type": "http", "headers": [(b"authorization", b"Bearer not-a-jwt")]})
    assert asyncio.run(routes.whoami(bad))["authenticated"] is False
    assert counter.calls == [{"reason": "invalid", "loom.transport": "http"}]


def test_mcp_refusals_before_dispatch_are_ledgered_as_the_token_alone(monkeypatch) -> None:
    """Refused before the caller is bound: each row is the token's org,
    subject and consumer with `-` for the rest and no arguments, whatever the
    call named, and carries nothing a batch's previous entry left behind."""
    import asyncio

    from server.auth.jwt import AuthInfo
    from server.observability import consumer, cost_accumulator, events
    from server.observability.run_context import set_ledger_run
    from server.operations.base import Role
    from server.transport import mcp_auth, mcp_call, mcp_op

    emitted: list = []

    class _Emitter:
        def emit(self, event) -> None:
            emitted.append(event)

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())
    eve = AuthInfo(
        subject="eve", org="eve-org", namespaces=(), roles=(Role.WRITER,), is_service_account=False
    )
    named = {"org": "acme", "namespace": "n", "user_id": "bob", "value": "secret text"}

    def _previous_entry() -> None:
        # What an earlier entry of the batch left: spend, a run, a consumer.
        cost_accumulator.begin_op()
        cost_accumulator.report_embed("text-embedding-3-small", 10_000)
        set_ledger_run("run-of-the-previous-entry")
        consumer.set_current_consumer("someone-else", "self-reported")

    def _call(name: str, arguments: dict) -> dict:
        return {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

    async def _no(*_a) -> bool:
        return False

    hidden = next(op for op in mcp_call.ALL_OPERATIONS if op.name == "blacklist.remove")
    refusals = [
        lambda: asyncio.run(mcp_auth._screen_tool_call(_call("loom_memory_set", dict(named)), eve)),
        lambda: asyncio.run(
            mcp_auth._screen_tool_call(_call("loom_memory_set", {**named, "org": "eve-org"}), eve)
        ),
        lambda: asyncio.run(mcp_call._resolve_tool(2, hidden.mcp_tool_name, eve, None)),
        lambda: mcp_op.route(
            _call("loom_op", {"op": "blacklist.remove", "arguments": dict(named)}),
            toolset=None,
            auth=eve,
        )[1],
        lambda: mcp_op.route(
            _call("loom_op", {"op": "memory.set", "arguments": dict(named)}),
            toolset="readonly",
            auth=eve,
        )[1],
    ]
    monkeypatch.setattr(mcp_auth, "_may_read", _no)
    for refuse in refusals:
        _previous_entry()
        assert refuse()
    # A name no op answers to writes no row.
    assert mcp_op.route(_call("loom_op", {"op": "no.such"}), toolset=None, auth=eve)[1]

    assert [(e.operation, e.error_code) for e in emitted] == [
        ("memory.set", "ORG_MISMATCH"),
        ("memory.set", "NAMESPACE_NOT_ACCESSIBLE"),
        ("blacklist.remove", "OP_NOT_ON_MCP"),
        ("blacklist.remove", "OP_NOT_ON_MCP"),
        ("memory.set", "UNKNOWN_OP"),
    ]
    for e in emitted:
        assert (e.org, e.namespace, e.agent, e.user_id, e.session_id) == (
            "eve-org",
            "-",
            "-",
            "-",
            "-",
        )
        assert e.subject == "eve" and e.consumer != "someone-else"
        assert e.upstream_cost_usd == 0.0 and e.run_id in ("", "-")
        assert "secret text" not in repr(e.args) and e.transport == "mcp"
        assert e.status_class == "client"


def test_an_http_refusal_naming_another_org_is_ledgered_in_the_token_s(monkeypatch) -> None:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import http

    bodies: list[dict] = []
    monkeypatch.setattr(http, "_emit_event", lambda op, rid, body, *a, **k: bodies.append(body))
    eve = AuthInfo(
        subject="eve", org="eve-org", namespaces=(), roles=(Role.WRITER,), is_service_account=False
    )
    op = OPERATIONS_BY_NAME["memory.get"]
    for org in ("acme", "eve-org"):
        call = http._HttpCall("r", raw_body={"org": org, "namespace": "n"}, auth=eve)
        http._refuse(op, call, 403, "AUTH_INSUFFICIENT")
    assert [(b["org"], b["namespace"]) for b in bodies] == [("eve-org", "-"), ("eve-org", "n")]
    # The unknown-argument refusal before authorization shares it; open mode keeps the body.
    assert http._refused_body({"org": "acme", "namespace": "n"}, eve)["org"] == "eve-org"
    assert http._refused_body({"org": "acme", "namespace": "n"}, None)["org"] == "acme"


def test_health_problems_need_no_clickhouse() -> None:
    from server.health import health_problems

    problems = health_problems()
    assert all({"code", "severity"} <= set(p) for p in problems)
    assert health_problem_series(problems)  # a test process runs on fake providers
