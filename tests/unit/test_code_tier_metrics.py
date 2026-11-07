"""Each failing code-index tier is a `loom.code.tier_failing` point (its failed passes in
a row, by tier and error kind) and a `loom.code.tier_lag_commits` point while its lag is
known, from the process's snapshot; the chart's alert rule reads those names with the
settings' default thresholds."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from server.code.app.index import tier_health as recorder
from server.code.domain.index import tier_health
from server.observability.metrics_gauges import code_tier_series
from server.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _row(**over: object) -> dict[str, object]:
    return {
        "org": "o",
        "namespace": "code-a",
        "tier": "clang",
        "kind": "credential",
        "failed_passes": 12,
        "lag_commits": 413,
        **over,
    }


def test_a_failing_tier_is_one_point_each_and_an_unknown_lag_none() -> None:
    failing, lag = code_tier_series(
        [_row(), _row(namespace="code-b", tier="fetch", lag_commits=-1)]
    )
    assert failing == [
        (12, {"loom.org": "o", "loom.namespace": "code-a", "tier": "clang", "kind": "credential"}),
        (12, {"loom.org": "o", "loom.namespace": "code-b", "tier": "fetch", "kind": "credential"}),
    ]
    assert lag == [(413, {"loom.org": "o", "loom.namespace": "code-a", "tier": "clang"})]


def test_the_namespace_label_is_clamped() -> None:
    (point,), _ = code_tier_series([_row(namespace="n" * 300)])
    assert point[1]["loom.namespace"] == "n" * 64


def test_the_rows_are_the_snapshots_failing_tiers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    health = tier_health.failed(None, "go", "fatal: Could not resolve host: h", T0)
    monkeypatch.setattr(recorder, "_FAILING", (recorder.FailingTier("o", "code-a", health, 7),))
    assert recorder.metric_rows() == (
        {
            "org": "o",
            "namespace": "code-a",
            "tier": "go",
            "kind": "network",
            "failed_passes": 1,
            "lag_commits": 7,
        },
    )
    monkeypatch.setattr(recorder, "_FAILING", None)
    assert recorder.metric_rows() == ()


def test_the_chart_rule_reads_the_gauges_with_the_settings_defaults() -> None:
    rule = (ROOT / "deploy/helm/loom/templates/prometheusrule.yaml").read_text()
    values = (ROOT / "deploy/helm/loom/values.yaml").read_text()
    gauges = (ROOT / "server/observability/metrics_gauges.py").read_text()
    for name in ("loom.code.tier_failing", "loom.code.tier_lag_commits"):
        assert f'name="{name}"' in gauges
        assert name.replace(".", "_") in rule
    fields = Settings.model_fields
    passes = re.search(r"failingPasses: (\d+)", values)
    lag = re.search(r"errorLagCommits: (\d+)", values)
    assert passes and int(passes.group(1)) == fields["code_tier_failing_warn_passes"].default
    assert lag and int(lag.group(1)) == fields["code_tier_failing_error_lag_commits"].default
