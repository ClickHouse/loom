"""server/observability/sli.py and the telemetry an alert rule reads:
op and status classes, the auth refusal reasons, the health-problem series."""

from __future__ import annotations

import asyncio
import contextvars
import re
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import revocation
from server.auth.bearer import bearer_token
from server.auth.jwt import AuthUnavailable, TokenError, mint_token, verify_token
from server.core.errors import BackendTimeoutError, RateLimitedError, ValidationError
from server.observability import sli
from server.observability.metrics import AUTH_REFUSAL_REASONS, auth_refusal_reason
from server.observability.metrics_gauges import health_problem_series
from server.operations.base import Role
from server.transport._common import sli_columns


def test_slo_classes() -> None:
    assert sli.slo_class("code.search", writes=False, admin=False) == "code"
    assert sli.slo_class("memory.search", writes=False, admin=False) == "search"
    assert sli.slo_class("memory.set", writes=True, admin=False) == "write"
    assert sli.slo_class("memory.get", writes=False, admin=False) == "read"
    assert sli.slo_class("grants.approve", writes=False, admin=True) == "admin"


def test_every_search_op_is_registered() -> None:
    """A renamed op would drop silently from the search SLO to read or write."""
    from server.operations.registry import OPERATIONS_BY_NAME

    assert sli._SEARCH_OPS <= set(OPERATIONS_BY_NAME)


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


def test_verify_token_tags_each_refusal_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reasons come from the raise sites, not from the test building the error."""
    secret = "unit-test-secret-for-refusal-reasons"
    monkeypatch.setattr(revocation, "_revoked_before", {})

    def refusal(token: str, revoked: frozenset[str] = frozenset()) -> str:
        monkeypatch.setattr(revocation, "_revoked_jtis", revoked)
        try:
            verify_token(token, secret=secret)
        except TokenError as e:
            return auth_refusal_reason(e)
        return "accepted"

    token = mint_token(
        "sa:bot", "acme", ["shop"], ["viewer"], secret=secret, jti="j1", session_id="s1"
    )
    assert refusal(token) == "accepted"
    assert refusal(token, frozenset({"j1"})) == "revoked"
    assert refusal(token, frozenset({"s1"})) == "revoked"
    assert refusal(token, frozenset({"sa:bot"})) == "revoked"
    expired = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=secret, ttl_seconds=-3600)
    assert refusal(expired) == "expired"
    assert refusal(token + "x") == "invalid"
    assert refusal("not-a-jwt") == "invalid"
    with pytest.raises(TokenError) as header:
        bearer_token("Basic abc")
    assert auth_refusal_reason(header.value) == "malformed"


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
    caller's; INTERNAL_ERROR is the server's, and so is HANDLER_HTTP_ERROR
    without the handler's exception, whose status is the only one that counts."""
    from server.core.paths import REPO_ROOT

    literals = set()
    for path in (REPO_ROOT / "server" / "transport").glob("*.py"):
        literals |= set(re.findall(r'error_code="([A-Z_0-9]+)"', path.read_text(encoding="utf-8")))
    assert literals, "no literal codes found: the pattern no longer matches the transports"
    for code in sorted(literals):
        expected = "server" if code in ("INTERNAL_ERROR", "HANDLER_HTTP_ERROR") else "client"
        assert sli.status_class(False, code, None) == expected, code


class _Counter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def add(self, n: int, attributes: dict[str, str]) -> None:
        self.calls.append(attributes)


def test_a_refused_bearer_is_counted_once_on_each_transport(monkeypatch) -> None:
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

    # A token that cannot be checked yet is a 503, counted once, also on the
    # auth-off service-account and setup-token routes.
    from server.identity.app.me_service_accounts import dev_bearer

    counter.calls.clear()
    monkeypatch.setattr(revocation, "_loaded", False)
    token = mint_token("sa:bot", "acme", ["shop"], ["viewer"])

    class _Unchecked:
        headers = {"Authorization": f"Bearer {token}"}

    for check in (lambda: bearer.verify_bearer(token), lambda: dev_bearer(_Unchecked())):
        with pytest.raises(HTTPException) as e:
            check()
        assert e.value.status_code == 503
    assert counter.calls == [{"reason": "unavailable", "loom.transport": "http"}] * 2

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
    from starlette.requests import Request

    from server.auth import routes
    from server.identity.app import me_callers, me_setup
    from server.insights.app import admin_routes
    from server.knowledge.app import download_routes
    from server.observability import metrics
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
    call named, and carries nothing a batch's previous entry left behind.
    Run in a copied context: the previous entry it stands up (a run, a
    consumer, spend) must not reach the tests after it."""
    contextvars.copy_context().run(_mcp_refusals_before_dispatch, monkeypatch)


def _mcp_refusals_before_dispatch(monkeypatch) -> None:
    from server.auth.jwt import AuthInfo
    from server.observability import consumer, cost_accumulator, events
    from server.observability.run_context import set_ledger_run
    from server.transport import access_refusal, mcp_auth, mcp_call, mcp_op
    from server.transport import namespace_resolve as nr

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
    # loom_op naming another org reaches the screen; a code index the token
    # may not read, and a namespace no index has.
    refusals += [
        lambda: asyncio.run(
            mcp_auth._screen_tool_call(
                mcp_op.route(
                    _call("loom_op", {"op": "memory.set", "arguments": dict(named)}),
                    toolset=None,
                    auth=eve,
                )[0],
                eve,
            )
        ),
        lambda: asyncio.run(
            mcp_auth._screen_tool_call(
                _call("loom_code_search", {"namespace": "code-x", "query": "q"}), eve
            )
        ),
        lambda: asyncio.run(
            mcp_auth._screen_tool_call(
                _call("loom_code_search", {"namespace": "nope", "query": "q"}), eve
            )
        ),
    ]
    monkeypatch.setattr(mcp_auth, "_may_read", _no)
    monkeypatch.setattr(nr, "known_namespaces", lambda org: {"code-x"})
    monkeypatch.setattr(nr, "match", lambda *a, **k: None)
    monkeypatch.setattr(nr, "default_branches", lambda org: {})
    monkeypatch.setattr(nr, "suggestable", lambda *a: [])
    monkeypatch.setattr(access_refusal, "reason_fields", lambda ns: {})
    for refuse in refusals:
        _previous_entry()
        assert refuse()
    # A name no op answers to writes no row.
    assert mcp_op.route(_call("loom_op", {"op": "no.such"}), toolset=None, auth=eve)[1]

    # The caller's context keeps what it had: the refusal reset only its own copy.
    assert consumer.current_consumer() == "someone-else"
    assert [(e.operation, e.error_code) for e in emitted] == [
        ("memory.set", "ORG_MISMATCH"),
        ("memory.set", "NAMESPACE_NOT_ACCESSIBLE"),
        ("blacklist.remove", "OP_NOT_ON_MCP"),
        ("blacklist.remove", "OP_NOT_ON_MCP"),
        ("memory.set", "UNKNOWN_OP"),
        ("memory.set", "ORG_MISMATCH"),
        ("code.search", "AUTH_INSUFFICIENT"),
        ("code.search", "UNKNOWN_NAMESPACE"),
    ]
    for e in emitted:
        assert (e.org, e.namespace, e.agent, e.user_id, e.session_id) == (
            "eve-org",
            "-",
            "-",
            "-",
            "-",
        )
        assert e.subject == "eve" and e.consumer == "user"  # the token's subject class
        assert e.upstream_cost_usd == 0.0 and e.run_id in ("", "-")
        assert "secret text" not in repr(e.args) and e.transport == "mcp"
        assert e.status_class == "client"


def test_a_wildcard_token_s_mcp_refusal_names_no_org_but_the_one_it_was_authorized_in(
    monkeypatch,
) -> None:
    from server.auth.jwt import AuthInfo
    from server.observability import events
    from server.transport import mcp_auth, mcp_op

    emitted: list = []

    class _Emitter:
        def emit(self, event) -> None:
            emitted.append(event)

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())

    async def _no(*_a) -> bool:
        return False

    monkeypatch.setattr(mcp_auth, "_may_read", _no)
    ops = AuthInfo(
        subject="ops", org="*", namespaces=(), roles=(Role.WRITER,), is_service_account=True
    )
    call = {"name": "loom_memory_set", "arguments": {"org": "acme", "namespace": "n"}}
    asyncio.run(mcp_auth._screen_tool_call({"id": 1, "method": "tools/call", "params": call}, ops))
    hidden = {"name": "loom_op", "arguments": {"op": "blacklist.remove"}}
    mcp_op.route({"id": 2, "method": "tools/call", "params": hidden}, toolset=None, auth=ops)
    # The screen authorized acme, as HTTP records it; before the screen no org is known.
    assert [(e.error_code, e.org) for e in emitted] == [
        ("NAMESPACE_NOT_ACCESSIBLE", "acme"),
        ("OP_NOT_ON_MCP", "-"),
    ]


def test_an_http_refusal_naming_another_org_is_ledgered_in_the_token_s(monkeypatch) -> None:
    from server.auth.jwt import AuthInfo
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
    # A body the input model refuses (no memory_key) names another org too.
    with pytest.raises(HTTPException):
        http._validate_payload(op, "r", {"org": "acme", "namespace": "n"}, eve)
    assert (bodies[-1]["org"], bodies[-1]["namespace"]) == ("eve-org", "-")

    # A misspelled write argument, refused while the body is read.
    class _Request:
        async def json(self) -> dict:
            return {"org": "acme", "namespace": "n", "memory_key": "k", "valu": "v"}

    with pytest.raises(HTTPException):
        asyncio.run(http._read_body(OPERATIONS_BY_NAME["memory.set"], _Request(), "r", eve))
    assert (bodies[-1]["org"], bodies[-1]["namespace"]) == ("eve-org", "-")
    # A code.* 422 also leaves a code-trace row, recorded the same way.
    traced: list[dict] = []
    monkeypatch.setattr(
        http, "schedule_code_trace_validation_failure", lambda op, body, errors: traced.append(body)
    )
    with pytest.raises(HTTPException):
        http._validate_payload(OPERATIONS_BY_NAME["code.search"], "r", {"org": "acme"}, eve)
    assert (traced[-1]["org"], traced[-1]["namespace"]) == ("eve-org", "-")
    # Open mode keeps the body.
    assert http._refused_body({"org": "acme", "namespace": "n"}, None)["org"] == "acme"


class _Meter:
    """Keeps each observable instrument's callback by name."""

    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}

    def _register(self, name: str, callbacks: list, **_: Any) -> None:
        self.callbacks[name] = callbacks[0]

    create_observable_gauge = create_observable_counter = _register


def test_lost_ledger_rows_are_counted_by_reason(monkeypatch) -> None:
    from server.observability import events, metrics_gauges

    class _Emitter:
        def stats(self) -> dict[str, int]:
            return {"dropped_queue_full": 3, "dropped_flush_failed": 5}

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    meter = _Meter()
    metrics_gauges._register_health_gauges(meter)
    points = meter.callbacks["loom.ledger.dropped"](None)
    assert {(p.attributes["reason"], p.value) for p in points} == {
        ("queue_full", 3),
        ("flush_failed", 5),
    }


def test_health_problems_need_no_clickhouse(monkeypatch) -> None:
    """The gauge answers with ClickHouse out of reach: a read there would fail
    the callback, which then exports nothing."""
    from server.core import ch
    from server.observability import metrics_gauges

    def _unreachable() -> Any:
        raise AssertionError("loom.health.problems read ClickHouse")

    monkeypatch.setattr(ch, "get_client", _unreachable)
    meter = _Meter()
    metrics_gauges._register_health_gauges(meter)
    points = meter.callbacks["loom.health.problems"](None)
    # A test process runs on fake providers, so there is always a problem.
    assert {p.attributes["code"] for p in points} >= {"fake_embedder", "fake_llm"}


def test_a_flood_of_unauthenticated_calls_writes_no_ledger_row(monkeypatch) -> None:
    """A 401 is counted on one closed label set and never reaches the ledger
    queue, so a flood of them adds no rows."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.observability import metrics
    from server.transport import http

    counter = _Counter()
    rows: list[Any] = []
    monkeypatch.setattr(metrics, "_auth_refusals", counter)
    monkeypatch.setattr(http, "emit_agent_event", lambda *a, **k: rows.append(k))
    monkeypatch.setattr(type(http.settings), "auth_actually_required", lambda self: True)
    app = FastAPI()
    app.include_router(http.build_router())
    client = TestClient(app)
    for i in range(200):
        headers = {"Authorization": f"Bearer junk-{i}"} if i % 2 else {}
        assert client.post("/v1/memory.get", json={"key": "k"}, headers=headers).status_code == 401
    assert rows == []
    assert len(counter.calls) == 200
    assert {c["reason"] for c in counter.calls} <= {"missing", "invalid", "malformed"}
    assert {c["loom.transport"] for c in counter.calls} == {"http"}


def test_otlp_metrics_reach_a_collector() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    from server.observability.otel import _make_metric_exporter

    received: list[tuple[str, bytes]] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        exporter = _make_metric_exporter("otlp", f"http://127.0.0.1:{server.server_port}/")
        reader = PeriodicExportingMetricReader(exporter, export_interval_millis=3_600_000)
        provider = MeterProvider(metric_readers=[reader])
        provider.get_meter("test").create_counter("loom.auth.refusals").add(
            1, {"reason": "missing"}
        )
        provider.force_flush()
        provider.shutdown()
    finally:
        server.shutdown()
    assert received and received[0][0] == "/v1/metrics"
    assert b"loom.auth.refusals" in received[0][1]
