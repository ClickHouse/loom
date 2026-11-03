"""server/observability/sli.py and the telemetry an alert rule reads:
op and status classes, the auth refusal reasons, the health-problem series."""

from __future__ import annotations

import asyncio
import contextvars
import re
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import delegation, revocation
from server.auth.bearer import bearer_token
from server.auth.jwt import AuthUnavailable, TokenError, mint_token, verify_token
from server.core.errors import BackendTimeoutError, RateLimitedError, ValidationError
from server.observability import sli
from server.observability.metrics import AUTH_REFUSAL_REASONS, auth_refusal_reason
from server.observability.metrics_gauges import health_problem_series
from server.operations.base import Role
from server.transport._common import sli_columns

# The real check: the unit conftest swaps in one that never refuses.
_REFUSE_A_REVOKED_PARENT = delegation._refuse_a_revoked_parent


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


def test_every_op_that_changes_state_is_in_the_write_slo() -> None:
    """Not only the writer-role ops: one a viewer may call that still writes
    (`namespace.restore`, `tracker.source.add`) must not burn the read SLO."""
    from server.operations.registry import ALL_OPERATIONS

    for op in ALL_OPERATIONS:
        got = sli_columns(op.name, "http")["slo_class"]
        if op.name.startswith("code.") or op.name in sli._SEARCH_OPS:
            continue
        if op.required_role >= Role.ADMIN:
            assert got == "admin", op.name
        else:
            assert got == ("read" if op.read_only else "write"), op.name
    assert sli_columns("namespace.restore", "http")["slo_class"] == "write"


def test_status_class_reads_the_unverified_mark_through_the_chain() -> None:
    marked = PermissionError("no grant could be read")
    marked.unverified = True  # type: ignore[attr-defined]
    raised = HTTPException(status_code=403)
    raised.__cause__ = marked
    assert sli.status_class(False, "AUTH_INSUFFICIENT", raised) == "server"

    # `from None` hides the context; a chain that loops ends.
    hidden = HTTPException(status_code=403)
    hidden.__context__, hidden.__suppress_context__ = marked, True
    assert sli.status_class(False, "AUTH_INSUFFICIENT", hidden) == "client"
    looped = PermissionError("x")
    looped.__cause__ = looped
    assert sli.status_class(False, "AUTH_INSUFFICIENT", looped) == "client"


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


def test_the_codes_the_transports_record_through_a_name_are_the_caller_s() -> None:
    """The literal-code test above cannot see codes recorded through a
    constant or a result body; each of these is a refusal the caller caused."""
    from server.transport import gates
    from server.transport import namespace_resolve as nr
    from server.transport.mcp_auth import _not_accessible_result
    from server.transport.mcp_op import UNKNOWN_OP

    codes = {
        UNKNOWN_OP,
        gates.ROLE_REFUSAL_CODE,
        "IDEMPOTENT_IN_PROGRESS",
        "IDEMPOTENT_CONFLICT",
        nr.unknown_result("nope", [])["structuredContent"]["code"],
        _not_accessible_result("loom_memory_get", None, "n")["structuredContent"]["code"],
        _not_accessible_result("loom_code_search", None, "code-x")["structuredContent"]["code"],
    }
    assert {c: sli.status_class(False, c, None) for c in codes} == dict.fromkeys(codes, "client")


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

    async def _no(*_a) -> tuple[bool, bool]:
        return False, False

    from server.operations.registry import OPERATIONS_BY_NAME

    hidden = OPERATIONS_BY_NAME["blacklist.remove"]
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

    async def _no(*_a) -> tuple[bool, bool]:
        return False, False

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
        http._refuse(op, call, 403, error_code="AUTH_INSUFFICIENT")
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
            return {
                "dropped_queue_full": 3,
                "dropped_flush_failed": 5,
                "dropped_batch_repeat": 2,
                "batch_repeats": 7,
            }

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    meter = _Meter()
    metrics_gauges._register_health_gauges(meter)
    points = meter.callbacks["loom.ledger.dropped"](None)
    assert {(p.attributes["reason"], p.value) for p in points} == {
        ("queue_full", 3),
        ("flush_failed", 5),
        ("batch_repeat", 2),
    }
    # A caller's repeated refusals are no loss, so a loss alert never sees them.
    assert [p.value for p in meter.callbacks["loom.ledger.batch_repeats"](None)] == [7]


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


def test_a_refusal_made_without_readable_grants_is_the_server_s(monkeypatch) -> None:
    """A grant, alias, account or classification read that fails still refuses
    (fail closed), but the refusal must not read as the caller's, or an outage
    of those reads leaves the SLO green."""
    from types import SimpleNamespace

    from server.auth import aliases, authz, delegation, service_accounts
    from server.auth.directory import ClassificationUnavailable
    from server.auth.jwt import AuthInfo
    from server.settings import settings

    class _Client:
        fails = ""  # a fragment of the one statement that fails
        grant = ""  # the role a grants read finds, if any

        async def query_one(self, sql: str, **k: Any) -> dict | None:
            if self.fails and self.fails in sql:
                raise ConnectionError("ClickHouse is down")
            if self.grant and "namespace_grants" in sql:
                return {"role": self.grant}
            return None

        async def query_all(self, sql: str, **k: Any) -> list:
            await self.query_one(sql)
            return []

    class _Directory:
        down = False

        async def is_restricted(self, org: str, namespace: str) -> bool:
            if self.down:
                raise ClassificationUnavailable(org)
            return False

        def code_entry(self, org: str, namespace: str) -> None:
            return None

    client, directory = _Client(), _Directory()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(authz, "directory", lambda: directory)

    def status(auth: AuthInfo, fails: str = "") -> str:
        client.fails = fails
        for cache in (aliases._CACHE, service_accounts._creators, delegation._account_active):
            cache.clear()
        with pytest.raises(PermissionError) as e:
            asyncio.run(authz.check_authorized(auth, Role.VIEWER, org="o", namespace="theirs"))
        return sli.status_class(False, "AUTH_INSUFFICIENT", e.value)

    def token(subject: str, **k: Any) -> AuthInfo:
        return AuthInfo(subject=subject, org="o", namespaces=["mine"], roles=[Role.VIEWER], **k)

    person = token("u@x.com", is_service_account=False)
    bot = token("sa:bot", is_service_account=True)
    delegated = token("u@x.com", is_service_account=False, creator="root@x.com")

    assert status(person) == "client"  # no grant: the caller's
    assert status(person, fails="namespace_grants") == "server"
    assert status(person, fails="contact_email") == "server"  # the aliases read
    assert status(bot, fails="service_accounts") == "server"
    assert status(delegated, fails="owned_live") == "server"  # the creator's account

    # A failed read stays noted after a check that passed despite it; the next
    # check starts clean, so its own plain refusal stays the caller's.
    async def _passed_then_refused() -> str:
        client.fails, client.grant = "contact_email", "viewer"  # admitted by a grant
        aliases._CACHE.clear()
        await authz.check_authorized(person, Role.VIEWER, org="o", namespace="theirs")
        client.fails, client.grant = "", ""
        with pytest.raises(PermissionError) as e:
            await authz.check_authorized(person, Role.VIEWER, org="o", namespace="theirs")
        return sli.status_class(False, "AUTH_INSUFFICIENT", e.value)

    assert asyncio.run(_passed_then_refused()) == "client"

    directory.down = True
    assert status(person) == "server"
    monkeypatch.setattr(settings, "restricted_serve", False)
    assert status(person) == "server"  # the kill switch refuses, the directory is down too

    # A creator account read while its aliases could not be: not cached.
    async def _read_account() -> None:
        client.fails = "contact_email"
        delegation._account_active.clear()
        await delegation._creator_account("o", "root@x.com")
        assert ("o", "root@x.com") not in delegation._account_active

    asyncio.run(_read_account())

    # The code-member rule runs a second gate after the first refused a write;
    # a read that second gate could not make marks the refusal the caller gets.
    from server.auth.fail_closed import marks_unverified, note_unreadable
    from server.code.app import member_writes

    @marks_unverified
    async def _gate(auth: Any, role: Role, **k: Any) -> None:
        if role == Role.VIEWER:
            note_unreadable()
        raise PermissionError("refused")

    async def _member_grant(*a: Any) -> Role:
        return Role.VIEWER

    monkeypatch.setattr(authz, "check_authorized", _gate)
    monkeypatch.setattr(authz, "_is_code_namespace", lambda org, ns: True)
    monkeypatch.setattr(authz, "_lookup_grant", _member_grant)
    op = SimpleNamespace(name="memory.set", required_role=Role.WRITER)
    with pytest.raises(PermissionError) as e:
        asyncio.run(member_writes.authorize_op(person, op, org="o", namespace="code-x"))
    assert sli.status_class(False, "AUTH_INSUFFICIENT", e.value) == "server"


def test_mcp_s_screen_records_an_unverifiable_refusal_as_the_server_s(monkeypatch) -> None:
    from server.auth.jwt import AuthInfo
    from server.observability import events
    from server.transport import mcp_auth

    emitted: list = []

    class _Emitter:
        def emit(self, event) -> None:
            emitted.append(event)

    async def _unreadable(*_a) -> tuple[bool, bool]:
        return False, True

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())
    monkeypatch.setattr(mcp_auth, "_may_read", _unreadable)
    eve = AuthInfo(
        subject="eve", org="eve-org", namespaces=(), roles=(Role.WRITER,), is_service_account=False
    )
    call = {"name": "loom_memory_set", "arguments": {"org": "eve-org", "namespace": "n"}}
    result = asyncio.run(
        mcp_auth._screen_tool_call({"id": 1, "method": "tools/call", "params": call}, eve)
    )
    # The caller is told the same; only the ledger knows it was the server.
    assert result["result"]["structuredContent"]["code"] == "NAMESPACE_NOT_ACCESSIBLE"

    # A code index name: one that names nothing is the caller's mistake
    # whatever the grants say; one resolved to an index is refused on that one.
    from server.transport import access_refusal
    from server.transport import namespace_resolve as nr

    monkeypatch.setattr(nr, "known_namespaces", lambda org: {"code-x"})
    monkeypatch.setattr(nr, "match", lambda ns, *a, **k: "code-x" if ns == "code-x-main" else None)
    monkeypatch.setattr(nr, "default_branches", lambda org: {})
    monkeypatch.setattr(nr, "suggestable", lambda *a: [])
    monkeypatch.setattr(access_refusal, "reason_fields", lambda ns: {})
    seen: list[str] = []

    async def _first_unreadable(auth: Any, org: str, ns: str) -> tuple[bool, bool]:
        seen.append(ns)
        return False, ns != "code-x"

    for ns in ("nope", "code-x-main"):
        call = {"name": "loom_code_search", "arguments": {"namespace": ns, "query": "q"}}
        result = asyncio.run(
            mcp_auth._screen_tool_call({"id": 2, "method": "tools/call", "params": call}, eve)
        )
    # The name given cannot be checked, but the one it resolves to is refused
    # outright: the refusal is that one's, the caller's.
    monkeypatch.setattr(mcp_auth, "_may_read", _first_unreadable)
    call = {"name": "loom_code_search", "arguments": {"namespace": "code-x-main", "query": "q"}}
    asyncio.run(mcp_auth._screen_tool_call({"id": 3, "method": "tools/call", "params": call}, eve))
    assert seen == ["code-x-main", "code-x"]
    assert [(e.error_code, e.status_class) for e in emitted] == [
        ("NAMESPACE_NOT_ACCESSIBLE", "server"),
        ("UNKNOWN_NAMESPACE", "client"),
        ("AUTH_INSUFFICIENT", "server"),
        ("AUTH_INSUFFICIENT", "client"),
    ]


def test_a_5xx_from_the_gate_is_ledgered_on_both_transports(monkeypatch) -> None:
    """A grants read past its stale window raises a 503 from inside the gate;
    it is answered as raised, and its row counts against the server."""
    from types import SimpleNamespace

    from fastapi import Response

    from server.auth.jwt import AuthInfo
    from server.core.errors import BackendUnavailableError
    from server.observability import events
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates, http, mcp_auth, mcp_call

    emitted: list = []

    class _Emitter:
        def emit(self, event) -> None:
            emitted.append(event)

    async def _unreadable(*a: Any, **k: Any) -> Any:
        raise BackendUnavailableError("grants unreadable")

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())
    monkeypatch.setattr(gates, "authorize", _unreadable)
    monkeypatch.setattr(mcp_auth, "_may_read", _unreadable)
    eve = AuthInfo(
        subject="eve",
        org="eve-org",
        namespaces=("n",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    op = OPERATIONS_BY_NAME["memory.get"]
    # HTTP's call names another org: the row is still the token's.
    payload = SimpleNamespace(org="acme", namespace="n")
    call = http._HttpCall(request_id="r", raw_body={"org": "acme", "namespace": "n"}, auth=eve)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(http._authorize(op, Response(), call, payload))
    with pytest.raises(BackendUnavailableError):
        asyncio.run(mcp_call._authorize_call(1, op, payload, {}, eve, "r"))
    screened = {"name": "loom_memory_get", "arguments": {"org": "eve-org", "namespace": "n"}}
    with pytest.raises(BackendUnavailableError):
        asyncio.run(
            mcp_auth._screen_tool_call({"id": 2, "method": "tools/call", "params": screened}, eve)
        )
    assert [(e.transport, e.error_code, e.status_class) for e in emitted] == [
        ("http", "BACKEND_UNAVAILABLE", "server"),
        ("mcp", "BACKEND_UNAVAILABLE", "server"),
        ("mcp", "BACKEND_UNAVAILABLE", "server"),
    ]
    assert "acme" not in {e.org for e in emitted}


def test_a_batch_of_refused_entries_writes_one_row_per_refusal(monkeypatch) -> None:
    """A single POST can carry hundreds of thousands of refused entries; within
    the batch each caller's refusal (op, code, org) is ledgered once, so they
    cannot fill the ledger queue for every tenant; a server fault is written
    a few times, since a caller cannot repeat one at will."""
    from server.auth.jwt import AuthInfo
    from server.observability import events
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp, mcp_auth, mcp_call, mcp_op
    from server.transport.mcp_ledger import emit_token_refusal

    emitted: list = []

    class _Emitter:
        batch_repeats = 0
        dropped_batch_repeat = 0

        def emit(self, event) -> None:
            emitted.append(event)

    emitter = _Emitter()
    monkeypatch.setattr(events, "get_emitter", lambda: emitter)
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: emitter)
    eve = AuthInfo(
        subject="eve", org="eve-org", namespaces=(), roles=(Role.WRITER,), is_service_account=False
    )
    hidden = {"name": "loom_op", "arguments": {"op": "blacklist.remove"}}
    foreign = {"name": "loom_memory_set", "arguments": {"org": "acme", "namespace": "n"}}
    misspelt = {"org": "eve-org", "namespace": "n", "memory_keyy": "k"}

    async def _entry(req: dict[str, Any]) -> None:
        params = req["params"]
        if params["name"] == "loom_op":
            mcp_op.route(req, toolset=None, auth=eve)
        elif params["name"] == "loom_memory_set":
            await mcp_auth._screen_tool_call(req, eve)
        else:  # refused by mcp_call before its handler, with the same code twice
            op = OPERATIONS_BY_NAME["memory.set"]
            mcp_call._emit_refused(op.name, misspelt, error_code="UNKNOWN_ARGUMENT", request_id="r")
            emit_token_refusal(op.name, eve, error_code="NAMESPACE_NOT_ACCESSIBLE")
            # A server fault is written up to ten times; the rest count as lost rows.
            emit_token_refusal(
                op.name, eve, error_code="NAMESPACE_NOT_ACCESSIBLE", server_fault=True
            )

    batch = [{"id": i, "method": "tools/call", "params": hidden} for i in range(1000)]
    batch += [{"id": i, "method": "tools/call", "params": foreign} for i in range(1000)]
    batch += [{"id": i, "method": "tools/call", "params": {"name": "x"}} for i in range(1000)]
    asyncio.run(mcp._dispatch_batch(batch, _entry))
    rows = sorted((e.operation, e.error_code, e.status_class) for e in emitted)
    assert sorted(set(rows)) == [
        ("blacklist.remove", "OP_NOT_ON_MCP", "client"),
        ("memory.set", "NAMESPACE_NOT_ACCESSIBLE", "client"),
        ("memory.set", "NAMESPACE_NOT_ACCESSIBLE", "server"),
        ("memory.set", "ORG_MISMATCH", "client"),
        ("memory.set", "UNKNOWN_ARGUMENT", "client"),
    ]
    assert len(rows) == 4 + 10  # each client refusal once, a server fault ten times
    # The rows not repeated: a caller's are its own doing; a server fault's are
    # lost server errors.
    assert emitter.batch_repeats == 4000 - 4
    assert emitter.dropped_batch_repeat == 1000 - 10
    # Outside a batch, each call is its own request.
    emitted.clear()
    emit_token_refusal("memory.set", eve, error_code="ORG_MISMATCH")
    emit_token_refusal("memory.set", eve, error_code="ORG_MISMATCH")
    assert len(emitted) == 2


def test_an_id_lookup_hidden_for_want_of_a_grants_read_is_the_server_s(monkeypatch) -> None:
    """`row_in_scope` answers found=False rather than 403 (a 403 would confirm
    the id), but when the refusal behind it could not be verified the row is
    the server's, or a grants outage reads as healthy id lookups."""
    from server.auth import authz
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import http

    rows: list[tuple[bool, str]] = []
    monkeypatch.setattr(
        http, "emit_agent_event", lambda **k: rows.append((k["success"], k["server_fault"]))
    )
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )

    async def _always_unverified(*a: Any, **k: Any) -> None:
        e = PermissionError("refused")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    async def _refused(unverified: bool) -> None:
        async def _check(*a: Any, **k: Any) -> None:
            e = PermissionError("refused")
            e.unverified = unverified  # type: ignore[attr-defined]
            raise e

        monkeypatch.setattr(authz, "check_authorized", _check)
        request_work.begin()
        set_current_auth(eve)
        # Run as handlers fan namespace checks out: in a child task.
        (hidden,) = await asyncio.gather(authz.row_in_scope("o", "other"))
        assert hidden is False
        http._emit_event(
            OPERATIONS_BY_NAME["memory.get_by_id"], "r", {}, 3, success=True, error_code=""
        )

    asyncio.run(_refused(unverified=True))
    asyncio.run(_refused(unverified=False))
    assert rows == [(True, True), (True, False)]

    # Elsewhere a refused namespace is one Loom added or filtered (code links,
    # repo docs, a trunk): the answer is whole without it.
    async def _additive() -> bool:
        request_work.begin()
        set_current_auth(eve)
        assert await authz.row_in_scope("o", "docs", hides_answer=False) is False
        from server.auth.fail_closed import answered_unverified

        return answered_unverified()

    monkeypatch.setattr(authz, "check_authorized", _always_unverified)
    assert asyncio.run(_additive()) is False


def test_inside_a_batch_a_call_s_refusal_is_ledgered_once_but_each_handler_failure_is(
    monkeypatch,
) -> None:
    from fastapi import HTTPException as _HTTPException
    from pydantic import BaseModel

    import server.code.app.member_writes as member_writes
    import server.transport.mcp_outcome as mcp_outcome
    from server.auth.jwt import AuthInfo
    from server.operations.base import Operation
    from server.transport import mcp_call
    from server.transport.mcp_ledger import one_refusal_row_each

    class _In(BaseModel):
        org: str
        namespace: str
        agent: str = "-"
        text: str = ""

    async def _fails(inp: Any) -> None:
        raise _HTTPException(status_code=500, detail={"code": "INTERNAL_ERROR"})

    op = Operation(
        name="test.batch_rows",
        input_model=_In,
        output_model=_In,
        handler=_fails,
        idempotent=True,
        required_role=Role.VIEWER,
    )

    async def _noop(*a: Any, **k: Any) -> None:
        return None

    rows: list[str] = []
    record = lambda name, args, ms, **k: rows.append(k["error_code"])  # noqa: E731
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_test_batch_rows", op)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _noop)
    monkeypatch.setattr(member_writes, "authorize_op", _noop)
    monkeypatch.setattr("server.transport.gates._admitted", _noop)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _noop)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", record)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", record)
    traces: list[str] = []
    monkeypatch.setattr(
        mcp_call, "schedule_code_trace_validation_failure", lambda name, *a: traces.append(name)
    )
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=True
    )
    args = {"org": "o", "namespace": "n", "text": "hi"}

    async def _batch() -> None:
        with one_refusal_row_each():
            for _ in range(3):
                params = {"name": "loom_test_batch_rows", "arguments": {**args, "text": 123}}
                await mcp_call._call_tool(1, params, auth=eve)
            for _ in range(2):
                params = {"name": "loom_test_batch_rows", "arguments": args}
                await mcp_call._call_tool(2, params, auth=eve)

    asyncio.run(_batch())
    assert rows.count("VALIDATION_FAILED") == 1
    assert len([c for c in rows if c != "VALIDATION_FAILED"]) == 2  # each failure its own row
    assert traces == ["test.batch_rows"]  # its code trace row, once too


def test_an_mcp_call_that_hid_a_row_unverified_is_the_server_s_and_the_next_starts_clean(
    monkeypatch,
) -> None:
    from pydantic import BaseModel

    import server.code.app.member_writes as member_writes
    from server.auth import authz
    from server.auth.jwt import AuthInfo
    from server.observability import events
    from server.operations.base import Operation
    from server.transport import mcp_call
    from server.transport.mcp_ledger import one_refusal_row_each

    class _In(BaseModel):
        org: str
        namespace: str
        agent: str = "-"
        hide: bool = False

    class _Out(BaseModel):
        found: bool

    async def _lookup(inp: Any) -> _Out:
        if inp.hide:  # as get_by_id hides a row the caller may not see
            return _Out(found=await authz.row_in_scope("o", "elsewhere"))
        return _Out(found=True)

    async def _unverified(*a: Any, **k: Any) -> None:
        e = PermissionError("refused")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    async def _noop(*a: Any, **k: Any) -> None:
        return None

    emitted: list = []

    class _Emitter:
        def emit(self, event) -> None:
            emitted.append(event)

    op = Operation(
        name="test.hidden_lookup",
        input_model=_In,
        output_model=_Out,
        handler=_lookup,
        idempotent=True,
        required_role=Role.VIEWER,
    )
    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_test_hidden_lookup", op)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _noop)
    monkeypatch.setattr(member_writes, "authorize_op", _noop)
    monkeypatch.setattr("server.transport.gates._admitted", _noop)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _noop)
    monkeypatch.setattr(authz, "check_authorized", _unverified)
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=True
    )

    async def _batch() -> None:
        with one_refusal_row_each():
            for hide in (True, False):
                args = {"org": "o", "namespace": "n", "hide": hide}
                await mcp_call._call_tool(
                    1, {"name": "loom_test_hidden_lookup", "arguments": args}, auth=eve
                )

    asyncio.run(_batch())
    assert [(e.success, e.status_class) for e in emitted] == [(True, "server"), (True, "ok")]


def test_a_listing_that_leaves_a_namespace_out_unverified_does_not_mark_the_request(
    monkeypatch,
) -> None:
    """MCP computes default namespaces on every call, often for namespaces the
    call never uses: a candidate refused unverified there must not make a
    correct answer a server error."""
    from types import SimpleNamespace

    from server.auth import authz, fail_closed
    from server.auth.jwt import AuthInfo
    from server.core import request_work

    async def _unverified(*a: Any, **k: Any) -> None:
        e = PermissionError("refused")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    async def _no_grants(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(rank=lambda ns: 0)

    async def _no_grant(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(authz, "check_authorized", _unverified)
    monkeypatch.setattr(authz.CachedGate, "grants", _no_grants)
    monkeypatch.setattr(authz.CachedGate, "_grant_role", _no_grant)
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )

    async def _listing() -> bool:
        request_work.begin()
        assert await authz.CachedGate(eve, "o").admits("elsewhere", Role.VIEWER) is False
        return fail_closed.answered_unverified()

    assert asyncio.run(_listing()) is False


def test_a_5xx_while_mcp_computes_the_caller_s_defaults_is_ledgered(monkeypatch) -> None:
    """MCP fills the caller's default namespace before the gate; a grants read
    past its stale window raises there, and must still leave a server row."""
    from types import SimpleNamespace

    from server.auth.jwt import AuthInfo
    from server.core.errors import BackendUnavailableError
    from server.observability import events
    from server.transport import mcp_call

    emitted: list = []

    class _Emitter:
        def emit(self, event) -> None:
            emitted.append(event)

    async def _unreadable(*a: Any, **k: Any) -> Any:
        raise BackendUnavailableError("grants unreadable")

    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.observability.ledger.get_emitter", lambda: _Emitter())
    monkeypatch.setattr("server.core.grant_cache.grant_map", _unreadable)
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )
    monkeypatch.setattr(mcp_call, "uuid", SimpleNamespace(uuid4=lambda: "the-call"))
    params = {"name": "loom_memory_get", "arguments": {"key": "k"}}  # no namespace: defaults run
    with pytest.raises(BackendUnavailableError):
        asyncio.run(mcp_call._call_tool(1, params, auth=eve))
    assert [e.request_id for e in emitted] == ["the-call"]  # joins its log lines
    # In the token's org: the call's identity is not filled in yet.
    assert [(e.operation, e.error_code, e.status_class, e.org) for e in emitted] == [
        ("memory.get", "BACKEND_UNAVAILABLE", "server", "o")
    ]


def test_only_answers_whole_without_the_namespace_leave_an_unverified_refusal_unmarked() -> None:
    """A refusal `row_in_scope` makes without a readable grant marks the request
    the server's unless the call site says the answer is whole without that
    namespace (one Loom adds on its own), the call filters a listing the
    caller did not scope, or another way in already admits the caller.
    Marking is the default, so a new call site counts an outage; this pins
    the opt-outs by file and function, and whether each is unconditional."""
    import ast

    from server.core.paths import REPO_ROOT

    unmarked: set[tuple[str, str, str]] = set()
    for path in (REPO_ROOT / "server").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, "id", getattr(call.func, "attr", ""))
                if name not in {
                    "row_in_scope",
                    "_in_scope",
                    "_gated",
                    "_may_read_tracker",
                    "authorized_tracker_namespace",
                    "authorized_tracker_namespaces",
                    "restricted_allows",
                }:
                    continue  # fmt: skip
                for kw in call.keywords:
                    if kw.arg != "hides_answer":
                        continue
                    rel = str(path.relative_to(REPO_ROOT))
                    if isinstance(kw.value, ast.Name) and kw.value.id == "hides_answer":
                        continue  # a gate passing its caller's choice through
                    if not isinstance(kw.value, ast.Constant):
                        unmarked.add((rel, fn.name, "conditional"))
                    elif kw.value.value is False:
                        unmarked.add((rel, fn.name, "never"))
    never, cond = "never", "conditional"
    assert unmarked == {
        # Namespaces Loom adds to the answer on its own.
        ("server/code/app/answers.py", "_inject_memory_context", never),
        ("server/code/app/answers_cache.py", "reuse_allowed", never),
        ("server/code/app/search_fanout.py", "readable_subset", never),
        ("server/memory/app/code_link_reads.py", "readable_namespaces", never),
        ("server/memory/app/code_grounding.py", "_authorized_code_target", never),
        ("server/code/app/repo_scope.py", "readable_repo_namespace", never),
        # The wording of a refusal another check decided.
        ("server/memory/app/decision_extract.py", "_readable_names", never),
        ("server/tracker/app/dupcheck_fanout.py", "readable_tracker_namespaces", never),
        ("server/tracker/app/common.py", "linked_trackers", never),
        ("server/auth/context.py", "restricted_namespaces_hidden_from", never),
        # A code answer the linked tracker only enriches.
        ("server/code/app/bisect_scan.py", "_linked_scan", never),
        ("server/code/app/blame.py", "_tracker_side", never),
        ("server/code/app/history.py", "_tracker_facts", never),
        ("server/code/app/symbol_history.py", "_tracker_side", never),
        ("server/memory/app/ask/records.py", "_append_tracker_facts", never),
        ("server/tracker/app/ask_route.py", "_searched_everywhere", never),
        ("server/tracker/app/ask_route.py", "qa_route_answer", never),
        # ...unless the caller named the tracker's item or the namespace.
        ("server/code/app/bisect.py", "_handle_bisect", cond),
        ("server/tracker/app/test_signal.py", "_code_namespace_authorized", cond),
        # A task's owner side, which marks only where it alone refused.
        ("server/tasks/app/common.py", "_owner_check", never),
        # A listing the caller did not scope.
        ("server/tasks/app/inboxes.py", "handle_inbox_list", never),
    }


def test_a_task_op_refused_on_its_own_side_without_a_readable_grant_is_the_server_s(
    monkeypatch,
) -> None:
    """A task op names its task by id: when the owner-side check could not be
    verified the 404 it answers is the server's, as the task reads' is."""
    from server.auth import authz
    from server.auth.context import set_current_auth
    from server.auth.fail_closed import answered_unverified
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.tasks.app import common

    async def _unverified(*a: Any, **k: Any) -> None:
        e = PermissionError("refused")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    monkeypatch.setattr(authz, "check_authorized", _unverified)
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.WRITER,), is_service_account=False
    )

    async def _side(**k: Any) -> bool:
        request_work.begin()
        set_current_auth(eve)
        assert await common._in_scope("o", "owner", write=True, **k) is False
        return answered_unverified()

    assert asyncio.run(_side()) is True
    assert asyncio.run(_side(hides_answer=False)) is False  # a listing's filter


def test_a_poster_s_task_op_stays_ok_when_the_owner_check_could_not_be_verified(
    monkeypatch,
) -> None:
    """The owner-side check marks only when it decides alone: a poster is
    admitted without a read, so its success stays ok; a refusal the owner
    check alone decides (anyone else, `task.wont_do`, an in-progress task)
    is the server's."""
    from types import SimpleNamespace

    from server.auth import authz, fail_closed
    from server.auth.context import set_current_auth
    from server.auth.fail_closed import answered_unverified
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.tasks.app import common, transitions

    async def _unverified(*a: Any, **k: Any) -> None:
        # As check_authorized does when a read it needed failed.
        fail_closed.note_unreadable()
        e = PermissionError("refused")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    status = {"value": "todo"}

    async def _task(org: str, task_id: str) -> dict[str, Any]:
        return {
            "org": "o",
            "namespace": "owner",
            "from_subject": "poster",
            "status": status["value"],
        }

    monkeypatch.setattr(authz, "check_authorized", _unverified)
    monkeypatch.setattr(transitions.repo, "fetch_task", _task)
    reason = SimpleNamespace(org="o", task_id="t", fence=None)

    def _as(subject: str) -> None:
        request_work.begin()
        set_current_auth(
            AuthInfo(
                subject=subject, org="o", namespaces=(), roles=(Role.WRITER,),
                is_service_account=False,
            )
        )  # fmt: skip

    async def _cancel_as_poster() -> tuple[bool, bool]:
        _as("poster")
        _row, owner_side, _unverified = await transitions._load_for_reason_op(reason)
        return owner_side, answered_unverified()

    async def _refused(subject: str, op: Any = transitions._load_for_reason_op) -> bool:
        _as(subject)
        with pytest.raises(Exception):  # noqa: B017 — the 404 or 409 the op answers
            await op(reason)
        return answered_unverified()

    wont_do = transitions.handle_task_wont_do  # only the owner side may finish it

    async def _read_as_poster() -> tuple[bool, bool]:
        _as("poster")
        return await common._readable(await _task("o", "t")), answered_unverified()

    assert asyncio.run(_cancel_as_poster()) == (False, False)
    assert asyncio.run(_refused("poster", wont_do)) is True  # the owner check alone refused
    status["value"] = "done"  # a finished task answers 409 whichever side asks
    assert asyncio.run(_refused("poster", wont_do)) is False
    status["value"] = "todo"
    assert asyncio.run(_read_as_poster()) == (True, False)
    assert asyncio.run(_refused("someone-else")) is True
    status["value"] = "in_progress"
    assert asyncio.run(_refused("poster")) is True  # only the owner side may act


def test_an_answer_hidden_whole_for_want_of_the_classification_is_the_server_s(
    monkeypatch,
) -> None:
    """With the classification unreadable, every namespace is hidden (`*`); the
    request that answered with nothing is the server's."""
    from server.auth import context
    from server.auth.directory import ClassificationUnavailable
    from server.auth.fail_closed import answered_unverified
    from server.auth.jwt import AuthInfo
    from server.core import request_work

    class _Down:
        def restricted_code_namespaces(self, org: str) -> set[str]:
            raise ClassificationUnavailable(org)

    monkeypatch.setattr(context, "directory", lambda: _Down())
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )

    async def _hidden() -> tuple[list[str], bool]:
        request_work.begin()
        context.set_current_auth(eve)
        return await context.restricted_namespaces_hidden_from("o"), answered_unverified()

    assert asyncio.run(_hidden()) == (["*"], True)


def test_an_org_rollup_is_marked_only_when_emptied_whole_for_want_of_a_read(
    monkeypatch,
) -> None:
    from server.auth import authz, context, fail_closed
    from server.auth.jwt import AuthInfo
    from server.core import request_work

    class _Directory:
        def restricted_code_namespaces(self, org: str) -> set[str]:
            return {"a-allowed", "b-secret"}

        async def restricted_set(self, org: str) -> list[str]:
            return []

    unreadable: set[str] = set()  # the namespaces whose grant read fails

    @fail_closed.marks_unverified  # as the real one is
    async def _check(auth: Any, role: Any, org: str, ns: str, grants: Any) -> None:
        if ns in unreadable:
            fail_closed.note_unreadable()
        if ns != "a-allowed":
            raise PermissionError(f"cannot access {ns}")

    monkeypatch.setattr(context, "directory", lambda: _Directory())
    monkeypatch.setattr(authz, "_check_restricted", _check)
    eve = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )

    async def _hidden(noted_before: bool = False) -> tuple[list[str], bool]:
        request_work.begin()
        context.set_current_auth(eve)
        if noted_before:  # a check that passed despite a failed read
            fail_closed.note_unreadable()
        return await context.restricted_namespaces_hidden_from(
            "o"
        ), fail_closed.answered_unverified()

    # A listing the caller did not scope: one namespace left out does not mark,
    # whether or not its read failed.
    assert asyncio.run(_hidden()) == (["b-secret"], False)
    assert asyncio.run(_hidden(noted_before=True)) == (["b-secret"], False)
    unreadable.update({"a-allowed", "b-secret"})
    assert asyncio.run(_hidden()) == (["b-secret"], False)

    # A member's org-wide scope emptied whole because a read failed is the
    # server's, as an admin's `*` is; emptied for want of a grant, the caller's.
    member = AuthInfo(
        subject="eve", org="o", namespaces=(), roles=(Role.VIEWER,), is_service_account=False
    )

    async def _member_scope(readable: list[str], read_fails: bool) -> tuple[bool, bool]:
        async def _mine(org: str) -> list[str]:
            if read_fails:
                fail_closed.note_unreadable()
            return readable

        monkeypatch.setattr(context, "resolve_my_namespaces", _mine)
        request_work.begin()
        context.set_current_auth(member)
        scope = await context.resolve_org_scope("o")
        return scope.accessible, fail_closed.answered_unverified()

    assert asyncio.run(_member_scope([], read_fails=True)) == (False, True)
    assert asyncio.run(_member_scope([], read_fails=False)) == (False, False)
    assert asyncio.run(_member_scope(["n"], read_fails=True)) == (True, False)


def test_http_s_ledger_note_of_a_handler_error_keeps_its_cause() -> None:
    from server.transport import http

    marked = PermissionError("no grant could be read")
    marked.unverified = True  # type: ignore[attr-defined]
    raised = HTTPException(status_code=403, detail={"code": "AUTH_INSUFFICIENT"})
    raised.__cause__ = marked
    note = http._http_exception_note(raised)
    assert sli.status_class(False, "HANDLER_HTTP_ERROR", note) == "server"


def test_audit_list_naming_a_restricted_namespace_it_cannot_verify_is_the_server_s(
    monkeypatch,
) -> None:
    """An org admin's `audit.list` naming one restricted namespace: its refusal
    is the whole answer, so it marks when the grant read failed."""
    from server.auth import authz, context, fail_closed
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit

    read_fails = {"value": False}

    @fail_closed.marks_unverified
    async def _check(*a: Any, **k: Any) -> None:
        if read_fails["value"]:
            fail_closed.note_unreadable()
        raise PermissionError("no grant on sec")

    restricted = {"value": True}

    async def _may_be_restricted(org: str, ns: str) -> bool:
        return restricted["value"]

    monkeypatch.setattr(authz, "_check_restricted", _check)
    monkeypatch.setattr(authz, "may_be_restricted", _may_be_restricted)
    admin = AuthInfo(
        subject="ada", org="o", namespaces=("*",), roles=(Role.ORG_ADMIN,), is_service_account=False
    )

    async def _list() -> tuple[int, bool]:
        request_work.begin()
        context.set_current_auth(admin)
        out = await audit.handle_audit_list(AuditListInput(org="o", namespace="sec"))
        return len(out.rows), fail_closed.answered_unverified()

    assert asyncio.run(_list()) == (0, False)  # no grant: the caller's
    read_fails["value"] = True
    assert asyncio.run(_list()) == (0, True)

    # A member naming a namespace it does not reach: marked only when a read
    # made while deciding it failed, not by one left from before the call.
    restricted["value"] = False
    member = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )
    mine_read_fails = {"value": False}

    async def _mine(org: str) -> list[str]:
        if mine_read_fails["value"]:
            fail_closed.note_unreadable()
        return []

    monkeypatch.setattr(context, "resolve_my_namespaces", _mine)

    async def _member_list(noted_before: bool = False) -> tuple[int, bool]:
        request_work.begin()
        context.set_current_auth(member)
        if noted_before:
            fail_closed.note_unreadable()
        out = await audit.handle_audit_list(AuditListInput(org="o", namespace="other"))
        return len(out.rows), fail_closed.answered_unverified()

    assert asyncio.run(_member_list()) == (0, False)
    assert asyncio.run(_member_list(noted_before=True)) == (0, False)
    mine_read_fails["value"] = True
    assert asyncio.run(_member_list()) == (0, True)


def test_the_member_scoped_org_rollups_mark_a_scope_emptied_by_a_failed_read(
    monkeypatch,
) -> None:
    """memory.owner_report and learnings.quality, scoped to a member's
    namespaces: empty for want of a read is the server's, for want of a
    grant the caller's."""
    from server.auth import context, fail_closed
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.memory.api.models_exposure import MemoryOwnerReportInput
    from server.memory.api.models_learnings import LearningsQualityInput
    from server.memory.app import exposure, learnings

    read_fails = {"value": False}

    async def _mine(org: str) -> list[str]:
        if read_fails["value"]:
            fail_closed.note_unreadable()
        return []

    monkeypatch.setattr(context, "resolve_my_namespaces", _mine)
    member = AuthInfo(
        subject="eve", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )

    async def _run(handler: Any, input_obj: Any) -> bool:
        request_work.begin()
        context.set_current_auth(member)
        await handler(input_obj)
        return fail_closed.answered_unverified()

    calls = [
        (exposure.handle_memory_owner_report, MemoryOwnerReportInput(org="o", user_id="eve")),
        (learnings.handle_learnings_quality, LearningsQualityInput(org="o")),
    ]
    for handler, input_obj in calls:
        read_fails["value"] = False
        assert asyncio.run(_run(handler, input_obj)) is False, handler.__name__
        read_fails["value"] = True
        assert asyncio.run(_run(handler, input_obj)) is True, handler.__name__


def test_a_call_served_on_its_default_namespace_counts_in_that_tenant(monkeypatch) -> None:
    """A code op naming no org or namespace (absent or empty) runs in the
    deployment's code namespace; its ledger row names that tenant on both
    transports, so the per-org rollup counts it there. One the gate refuses
    is the token's own org's row."""
    import dataclasses

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    import server.code.app.member_writes as member_writes
    import server.transport.http as http_mod
    from server.auth.jwt import AuthInfo
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.settings import settings
    from server.transport import gates, mcp_call

    class _In(BaseModel):
        org: str = ""
        namespace: str = ""

    class _Out(BaseModel):
        ok: bool = True

    async def _handler(inp: Any) -> _Out:
        return _Out()

    async def _noop(*a: Any, **k: Any) -> None:
        return None

    op = dataclasses.replace(
        OPERATIONS_BY_NAME["code.repos"], input_model=_In, output_model=_Out, handler=_handler
    )
    rows: list[dict[str, str]] = []
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-acme")
    monkeypatch.setattr(
        "server.observability.ledger.emit_agent_event",
        lambda **kw: rows.append(kw["identity"]),
    )
    monkeypatch.setattr(http_mod, "emit_agent_event", lambda **kw: rows.append(kw["identity"]))
    monkeypatch.setattr(gates, "rate_limit", lambda *a, **k: None)

    # HTTP, auth off.
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    r = TestClient(app).post("/v1/code.repos", json={})
    assert r.status_code == 200, r.text

    # MCP, a token admitted by the gate.
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, "loom_code_repos", op)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _noop)
    monkeypatch.setattr(member_writes, "authorize_op", _noop)
    monkeypatch.setattr("server.transport.gates._admitted", _noop)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _noop)
    eve = AuthInfo(
        subject="eve",
        org="eve-org",
        namespaces=("*",),
        roles=(Role.VIEWER,),
        is_service_account=True,
    )
    for arguments in ({}, {"org": "", "namespace": ""}):
        params = {"name": "loom_code_repos", "arguments": arguments}
        asyncio.run(mcp_call._call_tool(1, params, auth=eve))
    assert len(rows) == 3, rows
    assert all((row["org"], row["namespace"]) == ("acme", "code-acme") for row in rows), rows

    async def _refuse(*a: Any, **k: Any) -> None:
        raise PermissionError("no grant")

    rows.clear()
    monkeypatch.setattr(member_writes, "authorize_op", _refuse)
    asyncio.run(mcp_call._call_tool(1, {"name": "loom_code_repos", "arguments": {}}, auth=eve))
    assert [(row["org"], row["namespace"]) for row in rows] == [("eve-org", "-")]

    # HTTP with a token, through the endpoint: refused while its body is
    # validated (a null org) and at the gate, naming no org or an empty one.
    rows.clear()
    monkeypatch.setattr(http_mod, "_extract_auth", lambda request: eve)
    client = TestClient(app)
    assert client.post("/v1/code.repos", json={"org": None}).status_code == 422
    for body in ({}, {"org": "", "namespace": ""}, {"org": " "}, {"org": "-", "namespace": "n"}):
        assert client.post("/v1/code.repos", json=body).status_code == 403
    got = [(row["org"], row["namespace"]) for row in rows]
    # An absent org is the token's; a namespace the body named is kept.
    assert got == [("eve-org", "-")] * 4 + [("eve-org", "n")]


def test_the_ledger_reads_an_absent_org_or_namespace_as_the_gates_do() -> None:
    """Missing, empty, blank and "-" are one absent value: filled from the
    admitted target, else the token's org, else "-"."""
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core import request_work
    from server.transport._common import ledger_identity

    eve = AuthInfo(
        subject="eve", org="eve-org", namespaces=(), roles=(Role.VIEWER,), is_service_account=True
    )
    anyone = AuthInfo(
        subject="ops", org="*", namespaces=("*",), roles=(Role.VIEWER,), is_service_account=True
    )
    blank = {"org": " ", "namespace": ""}

    def _identity(auth: Any, admitted: tuple[str, str] | None) -> tuple[str, str]:
        set_current_auth(auth)
        request_work.begin().admitted = admitted
        out = ledger_identity(blank)
        return out["org"], out["namespace"]

    def run(auth: Any, admitted: tuple[str, str] | None = None) -> tuple[str, str]:
        return contextvars.copy_context().run(_identity, auth, admitted)

    assert run(eve, ("acme", "code-acme")) == ("acme", "code-acme")
    assert run(eve) == ("eve-org", "-")
    assert run(anyone) == ("-", "-")
    assert run(None) == ("-", "-")


def test_each_mcp_call_in_a_batch_starts_with_no_budget_shape() -> None:
    """A batch runs its entries in one task: a shape one handler noted must
    not price the next entry's budget (its ledger row's `budget_ms`)."""
    from server.observability.budget_shape import current_budget_shape, note_budget_shape
    from server.transport._common import begin_call

    def _two_calls() -> str:
        begin_call()
        note_budget_shape("hybrid")
        begin_call()
        return current_budget_shape()

    assert contextvars.copy_context().run(_two_calls) == ""


def test_a_mint_by_a_revoked_token_and_a_refused_scim_bearer_are_counted(monkeypatch) -> None:
    """Two refusals of the caller's bearer outside `verify_bearer`: a mint whose
    token ClickHouse says is revoked (this replica's snapshot may not know yet),
    and a SCIM request without the provider's token."""
    from server.auth import scim_routes
    from server.auth.jwt import AuthInfo
    from server.observability import metrics

    counter = _Counter()
    monkeypatch.setattr(metrics, "_auth_refusals", counter)

    async def _cutoffs(subjects: list[str]) -> dict[str, int | None]:
        return {"alice": None}

    monkeypatch.setattr(revocation, "cutoffs", _cutoffs)
    alice = AuthInfo(
        subject="alice", org="o", namespaces=("n",), roles=(Role.WRITER,), is_service_account=False
    )
    with pytest.raises(HTTPException) as e:
        asyncio.run(_REFUSE_A_REVOKED_PARENT(alice, 1, []))
    assert e.value.status_code == 401

    monkeypatch.setattr(scim_routes.settings, "scim_token", "s" * 40)

    class _Request:
        method = "GET"

        def __init__(self, authorization: str) -> None:
            self.headers = {"authorization": authorization} if authorization else {}

    for header in ("", "Basic abc", "Bearer ", "Bearer " + "x" * 40):
        with pytest.raises(scim_routes.ScimError):
            asyncio.run(scim_routes._authorized(_Request(header)))
    reasons = [c["reason"] for c in counter.calls]
    assert reasons == ["revoked", "missing", "malformed", "malformed", "invalid"]
    assert [c["loom.transport"] for c in counter.calls] == ["http"] + ["scim"] * 4
