"""A code-index tier that keeps failing is one `code_tier_failing` health problem, fed
from a per-process snapshot of the tier-health records (never a ClickHouse read in the
probe): warn past the pass or hour threshold, error when also far behind, with the
entry, tier, kind, since, lag and fix."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.app.index import tier_health as recorder
from server.code.domain.index import tier_health
from server.observability.problems import HealthState, collect_problems
from server.settings import settings

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
NO_TOKEN = (
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
    "fatal: clone of 'https://github.com/o/private.git' into submodule path 'contrib/p' failed"
)


def _failing(passes: int, since: datetime = T0) -> tier_health.TierHealth:
    health = tier_health.failed(None, "clang", NO_TOKEN, since)
    for i in range(1, passes):
        health = tier_health.failed(health, "clang", NO_TOKEN, since + timedelta(minutes=30 * i))
    return health


def _level(health: tier_health.TierHealth, lag: int, now: datetime) -> str:
    return tier_health.severity(
        health, lag, now, warn_passes=3, warn_hours=3.0, error_lag_commits=100
    )


def test_one_or_two_failures_are_transient_and_raise_nothing() -> None:
    assert _level(_failing(2), 413, T0 + timedelta(hours=1)) == ""
    assert _level(tier_health.recovered("clang", T0), 413, T0) == ""


def test_the_third_failure_in_a_row_warns_and_a_far_behind_tier_is_an_error() -> None:
    now = T0 + timedelta(hours=1)
    assert _level(_failing(3), 5, now) == "warn"
    assert _level(_failing(3), -1, now) == "warn"  # unknown lag is never an error
    assert _level(_failing(3), 100, now) == "error"


def test_a_tier_failing_for_hours_warns_however_few_passes_ran() -> None:
    assert _level(_failing(1), 0, T0 + timedelta(hours=4)) == "warn"


@pytest.mark.parametrize(
    ("tier", "lag"),
    [("ingest", 7), ("clang", 413), ("clang_full", 413), ("python", -1), ("fetch", -1)],
)
def test_the_lag_comes_from_the_published_status(tier: str, lag: int) -> None:
    snap = {"fast_tier_lag": 7, "compiler_tiers": [{"producer": "clang", "lag": 413}]}
    assert tier_health.snapshot_lag(snap, tier) == lag


class _Store:
    def __init__(self, rows: list[dict[str, Any]], snaps: dict[tuple[str, str], str]) -> None:
        self.rows, self.snaps = rows, snaps
        self.fail = False

    async def state_rows_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("clickhouse down")
        return [r for r in self.rows if str(r["state_key"]).startswith(prefix)]

    async def state_value_for_pairs(
        self, pairs: list[tuple[str, str]], key: str
    ) -> dict[tuple[str, str], str]:
        assert key == "status_snapshot"
        return {p: v for p, v in self.snaps.items() if p in pairs}


def _row(ns: str, tier: str, health: tier_health.TierHealth) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": ns,
        "state_key": f"tier_health:{tier}",
        "state_value": health.to_json(),
    }


@pytest.mark.asyncio
async def test_the_snapshot_holds_the_failing_tiers_with_their_lag_and_keeps_it_on_a_failed_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(
        [
            _row("code-a", "clang", _failing(4)),
            _row("code-b", "fetch", tier_health.recovered("fetch", T0)),
        ],
        {("o", "code-a"): json.dumps({"compiler_tiers": [{"producer": "clang", "lag": 413}]})},
    )
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda ch=None: store)
    recorder.reset_for_tests()
    assert recorder.problem_rows() is None  # not read yet: unknown, not healthy
    await recorder.refresh_failing()
    snap = recorder.failing_snapshot()
    assert snap is not None and [(f.namespace, f.health.tier, f.lag_commits) for f in snap] == [
        ("code-a", "clang", 413)
    ]
    store.fail = True
    await recorder.refresh_failing()
    assert recorder.failing_snapshot() == snap
    rows = recorder.problem_rows(T0 + timedelta(hours=2))
    assert rows is not None and rows[0]["severity"] == "error" and rows[0]["kind"] == "credential"
    recorder.reset_for_tests()


def _problem(rows: tuple[dict[str, Any], ...] | None) -> list[Any]:
    return [
        p
        for p in collect_problems(settings, HealthState(code_tier_failures=rows))
        if p.code == "code_tier_failing"
    ]


def test_the_problem_names_the_entry_tier_kind_since_lag_and_fix() -> None:
    row = {
        "org": "o",
        "namespace": "code-a",
        "tier": "clang",
        "kind": "credential",
        "since": "2026-01-01T12:00:00+00:00",
        "failed_passes": 12,
        "lag_commits": 413,
        "fix": "No credential with access (github.com/o/private): set LOOM_CODE_GITHUB_TOKEN.",
        "severity": "error",
    }
    (p,) = _problem((row,))
    assert p.severity == "error" and p.source == "workers"
    assert (
        "o/code-a clang (credential, 12 failed passes since 2026-01-01T12:00:00+00:00" in p.message
    )
    assert "413 commits behind" in p.message
    assert p.fix == row["fix"]


def test_many_failing_tiers_are_one_problem_with_each_fix_once() -> None:
    base = {"org": "o", "tier": "fetch", "kind": "network", "since": "s", "failed_passes": 3}
    rows = tuple(
        {
            **base,
            "namespace": f"code-{i}",
            "lag_commits": -1,
            "fix": "Check DNS.",
            "severity": "warn",
        }
        for i in range(7)
    )
    (p,) = _problem(rows)
    assert p.severity == "warn" and p.fix == "Check DNS."
    assert "7 code-index tiers are failing" in p.message and "and 2 more" in p.message
    assert "commits behind" not in p.message


@pytest.mark.parametrize("rows", [None, ()])
def test_no_failing_tier_is_no_problem(rows: tuple[dict[str, Any], ...] | None) -> None:
    assert _problem(rows) == []
