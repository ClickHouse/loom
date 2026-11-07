"""Each tier the `code_tier_failing` health problem reports is a `loom.code.tier_failing`
point (its failed passes in a row, by tier, error kind and the problem's severity) and a
`loom.code.tier_lag_commits` point while its lag is known, from the process's snapshot;
the chart's rule and the dashboard alert read those points, so their thresholds are the
problem's settings."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from server.code.app.index import tier_health as recorder
from server.code.domain.index import tier_health
from server.core.datetimes import utc_now
from server.observability import metrics_gauges
from server.observability.metrics_gauges import code_tier_series
from server.observability.state_inputs import state_inputs
from server.settings import settings

ROOT = Path(__file__).resolve().parents[2]


def _row(**over: object) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "code-a",
        "tier": "clang",
        "kind": "credential",
        "failed_passes": 12,
        "lag_commits": 413,
        "severity": "error",
        **over,
    }


def test_a_reported_tier_is_one_point_each_and_an_unknown_lag_none() -> None:
    failing, lag = code_tier_series(
        [_row(), _row(namespace="code-b", tier="fetch", lag_commits=-1, severity="warn")]
    )
    attrs = {"loom.org": "o", "loom.namespace": "code-a", "tier": "clang"}
    assert failing == [
        (12, {**attrs, "kind": "credential", "severity": "error"}),
        (
            12,
            {
                "loom.org": "o",
                "loom.namespace": "code-b",
                "tier": "fetch",
                "kind": "credential",
                "severity": "warn",
            },
        ),
    ]
    assert lag == [(413, attrs)]


@pytest.fixture
def _snapshot() -> Any:
    yield
    recorder.reset_for_tests()


def _failing(passes: int, hours_ago: float) -> tier_health.TierHealth:
    since = utc_now() - timedelta(hours=hours_ago)
    health = None
    for _ in range(passes):
        health = tier_health.failed(health, "clang", "fatal: could not read Username", since)
    assert health is not None
    return health


def test_the_wired_rows_are_the_tiers_the_problem_reports(
    _snapshot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "code_tier_failing_warn_passes", 3)
    monkeypatch.setattr(settings, "code_tier_failing_warn_hours", 3.0)
    monkeypatch.setattr(settings, "code_tier_failing_error_lag_commits", 100)
    recorder._FAILING = (
        recorder.FailingTier("o", "code-new", _failing(1, 0.5), 413),  # transient: none
        recorder.FailingTier("o", "code-slow", _failing(1, 4), 5),  # 4 hours: warn
        recorder.FailingTier("o", "code-far", _failing(3, 2), 413),  # error
    )
    failing, _ = code_tier_series(state_inputs().failing_code_tiers())
    assert sorted((a["loom.namespace"], a["severity"]) for _, a in failing) == [
        ("code-far", "error"),
        ("code-slow", "warn"),
    ]
    recorder.reset_for_tests()
    assert list(state_inputs().failing_code_tiers()) == []  # not read yet


class _Meter:
    def __init__(self) -> None:
        self.units: dict[str, str] = {}

    def create_observable_gauge(
        self, *, name: str, callbacks: Any, description: str = "", unit: str = ""
    ) -> None:
        self.units[name] = unit


def test_the_gauges_register_with_the_names_the_alerts_read() -> None:
    meter = _Meter()
    metrics_gauges._register_code_tier_gauges(meter)
    # annotation units: an OTLP-to-Prometheus export adds no unit suffix to the names
    assert meter.units == {
        "loom.code.tier_failing": "{pass}",
        "loom.code.tier_lag_commits": "{commit}",
    }
    rule = (ROOT / "deploy/helm/loom/templates/prometheusrule.yaml").read_text()
    assert "loom_code_tier_failing)" in rule and 'loom_code_tier_failing{severity="error"}' in rule


def test_the_dashboard_alert_fires_on_any_reported_tier() -> None:
    spec = json.loads((ROOT / "deploy/dashboards/code_plane.json").read_text())
    tile = next(t for t in spec["tiles"] if t["id"] == "tier_failing")
    assert "MetricName = 'loom.code.tier_failing'" in tile["sql"]
    assert tile["alert"]["condition"] == "max(Value) > 0 for 15m"
