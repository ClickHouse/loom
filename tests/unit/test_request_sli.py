"""server/observability/sli.py and the telemetry that feeds the alert rules:
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


def test_health_problems_need_no_clickhouse() -> None:
    from server.health import health_problems

    problems = health_problems()
    assert all({"code", "severity"} <= set(p) for p in problems)
    assert health_problem_series(problems)  # a test process runs on fake providers
