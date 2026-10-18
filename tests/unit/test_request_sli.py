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
        cls = sli.op_class(
            op.name,
            writes=op.required_role == Role.WRITER,
            admin=op.required_role >= Role.ADMIN,
        )
        assert cls in sli.OP_CLASSES, op.name


def test_op_classes() -> None:
    assert sli.op_class("code.search", writes=False, admin=False) == "code"
    assert sli.op_class("memory.search", writes=False, admin=False) == "search"
    assert sli.op_class("memory.set", writes=True, admin=False) == "write"
    assert sli.op_class("memory.get", writes=False, admin=False) == "read"
    assert sli.op_class("grants.approve", writes=False, admin=True) == "admin"


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
    assert cols["transport"] == "mcp" and cols["op_class"] == "read"
    assert cols["budget_ms"] > 0
    assert sli_columns("no.such.op", "http") == {"transport": "http"}


def test_auth_refusal_reasons_are_bounded() -> None:
    cases = {
        None: "missing",
        AuthUnavailable("revocation list not loaded"): "unavailable",
        TokenError("authorization header must be `Bearer <token>`"): "malformed",
        TokenError("token expired: Signature has expired"): "expired",
        TokenError("token jti 'x' is revoked"): "revoked",
        TokenError("invalid token: bad signature"): "invalid",
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
