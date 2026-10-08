"""Per-namespace code-plane gauges.

Every gauge (`loom.code.index_age_seconds`, `scip_age_seconds`, the Python
tier, the PR cursor, index errors, the embed backlog, symbol counts) is read
per namespace, so a stalled repo is not hidden behind a healthy one. The
probe is one UNION ALL query; this pins the pure transform of its rows and
the gauge registration/attributes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from server.observability import metrics_gauges, otel_exporters, otel_probes

NOW = dt.datetime(2026, 9, 9, 12, 0, 0, tzinfo=dt.UTC)


def _ts(seconds_ago: float) -> float:
    return (NOW - dt.timedelta(seconds=seconds_ago)).timestamp()


def test_code_series_from_rows_per_namespace() -> None:
    rows = [
        ("acme", "code-a", "state:git_head", _ts(60), "abc"),
        ("acme", "code-b", "state:git_head", _ts(3600), "def"),  # the stalled one
        ("acme", "code-a", "state:scip_commit", _ts(7200), "abc"),
        ("acme", "code-a", "state:scip_commit_py", _ts(300), "abc"),
        # pr_cursor: the VALUE is the datetime that matters, not the row's updated_at
        ("acme", "code-a", "state:pr_cursor", _ts(10), "2026-09-09T11:00:00+00:00"),
        # unparsable cursor falls back to updated_at
        ("acme", "code-b", "state:pr_cursor", _ts(20), "not-a-date"),
        ("acme", "code-a", "state:last_refresh_at", _ts(1), "ignored"),
        ("acme", "code-a", "files_errored", 7.0, ""),
        ("acme", "code-b", "files_errored", 0.0, ""),
        ("acme", "code-a", "embed_backlog", 1234.0, ""),
        ("acme", "code-a", "symbols:treesitter", 100000.0, ""),
        ("acme", "code-a", "symbols:scip", 250000.0, ""),
        ("acme", "code-a", "symbols:weird", 1.0, ""),
    ]
    out = otel_probes._code_series_from_rows(rows, now=NOW)
    a = {"loom.org": "acme", "loom.namespace": "code-a"}
    b = {"loom.org": "acme", "loom.namespace": "code-b"}

    assert out["code_index_age_seconds"] == [(60.0, a), (3600.0, b)]
    assert out["code_scip_age_seconds"] == [(7200.0, a)]
    assert out["code_scip_py_age_seconds"] == [(300.0, a)]
    assert out["code_pr_cursor_age_seconds"] == [(3600.0, a), (20.0, b)]
    assert out["code_files_errored"] == [(7.0, a), (0.0, b)]
    assert out["code_embed_backlog"] == [(1234.0, a)]
    assert out["code_symbols"] == [
        (100000.0, {**a, "tier": "treesitter"}),
        (250000.0, {**a, "tier": "scip"}),
        (1.0, {**a, "tier": "other"}),
    ]
    assert "last_refresh_at" not in str(out)


def test_ages_never_negative() -> None:
    rows = [("o", "n", "state:git_head", (NOW + dt.timedelta(seconds=30)).timestamp(), "")]
    assert otel_probes._code_series_from_rows(rows, now=NOW)["code_index_age_seconds"] == [
        (0.0, {"loom.org": "o", "loom.namespace": "n"})
    ]


def test_probe_state_no_longer_carries_the_global_code_readings() -> None:
    """The scalar readings moved to the per-namespace probe; nothing may keep
    a deployment-wide max() alive that masks a stalled second repo."""
    import inspect

    src = inspect.getsource(otel_probes._probe_state)
    assert "code_index_state" not in src
    assert "code_head_updated" not in src


class _Meter:
    def __init__(self) -> None:
        self.gauges: dict[str, Any] = {}
        self.descriptions: dict[str, str] = {}

    def create_observable_gauge(
        self, *, name: str, callbacks: Any, description: str = "", unit: str = ""
    ) -> None:
        self.gauges[name] = callbacks[0]
        self.descriptions[name] = description


def test_code_gauges_registered_with_stable_names_and_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = {
        "code_index_age_seconds": [(12.0, {"loom.org": "o", "loom.namespace": "n"})],
        "code_symbols": [(5.0, {"loom.org": "o", "loom.namespace": "n", "tier": "scip"})],
    }
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict, code_probe=lambda: series)

    # Names are stable (existing panels keep working) and the new ones exist.
    for name in (
        "loom.code.index_age_seconds",
        "loom.code.scip_age_seconds",
        "loom.code.scip_py_age_seconds",
        "loom.code.pr_cursor_age_seconds",
        "loom.code.files_errored",
        "loom.code.embed_backlog",
        "loom.code.symbols",
        "loom.worker.consecutive_failures",
    ):
        assert name in meter.gauges, name

    # The TTL cache produces in a background thread; the first read is empty
    # and must render as "no observation", never 0.
    assert meter.gauges["loom.code.index_age_seconds"](None) == []
    import time

    for _ in range(50):
        obs = meter.gauges["loom.code.index_age_seconds"](None)
        if obs:
            break
        time.sleep(0.02)
    (o,) = obs
    assert o.value == 12.0 and o.attributes == {"loom.org": "o", "loom.namespace": "n"}
    (sym,) = meter.gauges["loom.code.symbols"](None)
    assert sym.attributes["tier"] == "scip"
    # A key the probe did not return yields no observation, not a zero.
    assert meter.gauges["loom.code.embed_backlog"](None) == []


def test_without_a_code_probe_the_code_gauges_stay_silent() -> None:
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict)
    assert meter.gauges["loom.code.index_age_seconds"](None) == []


def test_edge_quality_rows_become_per_namespace_series() -> None:
    import datetime as _dt

    from server.observability.otel_probes import _code_series_from_rows

    rows = [
        ("o", "n", "edges_unqualified", 9352.0, ""),
        ("o", "n", "edges_low_confidence", 12725.0, ""),
        ("o", "clean", "edges_unqualified", 0.0, ""),
    ]
    out = _code_series_from_rows(rows, now=_dt.datetime.now(_dt.UTC))
    assert out["code_edges_unqualified"] == [
        (9352.0, {"loom.org": "o", "loom.namespace": "n"}),
        (0.0, {"loom.org": "o", "loom.namespace": "clean"}),
    ], "a clean namespace reads 0, it does not vanish"
    assert out["code_edges_low_confidence"] == [(12725.0, {"loom.org": "o", "loom.namespace": "n"})]


def test_code_counts_refresh_on_their_own_clock(monkeypatch) -> None:
    """The watermark query runs every probe; the FINAL counts query runs once
    per LOOM_OTEL_CODE_COUNTS_CACHE_S."""
    from server.settings import settings

    calls: list[str] = []

    class _Res:
        result_rows: list = []

    class _Client:
        def query(self, sql: str, *a, **k):
            calls.append("counts" if "code_symbols FINAL" in sql else "state")
            return _Res()

    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: _Client())
    monkeypatch.setattr(settings, "otel_code_counts_cache_s", 900)
    # the grep wrong-abstain probe has its own clock, tested in
    # test_grep_wrong_abstain_probe.py; off here so only the two clocks under
    # test issue statements
    monkeypatch.setattr(settings, "otel_code_grep_replay_cache_s", 0)
    otel_probes._CODE_COUNTS_CACHE["at"] = 0.0
    otel_probes._CODE_COUNTS_CACHE["rows"] = []
    otel_probes._probe_code_state()
    otel_probes._probe_code_state()
    assert calls == ["state", "counts", "state"]
    # 0 = every call
    monkeypatch.setattr(settings, "otel_code_counts_cache_s", 0)
    otel_probes._probe_code_state()
    assert calls == ["state", "counts", "state", "state", "counts"]


def test_retention_series_per_org_and_gauges_registered() -> None:
    rows = [
        ("dev", 1000, 800, 760, 12, 40, 300),
        ("acme", 50, 0, 0, 0, 3, 0),
    ]
    series = otel_probes._retention_series_from_rows(rows)
    dev = {k: v[0] for k, v in series.items()}
    assert dev["retention_active_rows"] == (1000.0, {"loom.org": "dev"})
    assert dev["retention_never_recalled_ratio"][0] == pytest.approx(0.95)
    assert dev["retention_low_information_rows"][0] == 12.0
    assert dev["retention_sensitive_without_expiry"][0] == 40.0
    assert dev["retention_stale_candidates"][0] == 300.0
    # no rows older than 7 days: the ratio is 0, not a division error
    assert series["retention_never_recalled_ratio"][1] == (0.0, {"loom.org": "acme"})

    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict, retention_probe=lambda: series)
    for name in (
        "loom.retention.active_rows",
        "loom.retention.never_recalled_ratio",
        "loom.retention.low_information_rows",
        "loom.retention.sensitive_without_expiry",
        "loom.retention.stale_candidates",
    ):
        assert name in meter.gauges, name


def test_retention_sql_carries_the_stale_age_rule() -> None:
    sql = otel_probes._RETENTION_SQL
    assert "FROM loom.memory FINAL" in sql and "GROUP BY org" in sql
    assert "memory_type NOT IN ('preference', 'procedural')" in sql
    assert "pinned = 0" in sql and "{stale:UInt32} DAY" in sql
    assert "coalesce(mentioned_at, created_at)" in sql and "valid_at" not in sql
    assert "sensitivity != '' AND expires_at IS NULL" in sql
    assert "value.size < 25" in sql  # length(value), read from the size stream


def test_code_counts_probe_scans_the_parser_call_edges_once() -> None:
    """The two edge-quality counts ride one FINAL scan of loom.code_edges,
    unfolded into the same (k, v) rows — the same scan twice would be about
    two thirds of the probe's bytes per call. FINAL stays on every leg:
    without it the counts over-count by up to 575%."""
    sql = otel_probes._CODE_COUNTS_SQL
    assert sql.count("FROM loom.code_edges") == 1
    assert sql.count("FROM loom.code_edges FINAL") == 1
    assert "tuple('edges_unqualified', unqualified)" in sql
    assert "tuple('edges_low_confidence', low_confidence)" in sql
    assert "ARRAY JOIN" in sql and "kv.1 AS k, kv.2 AS v" in sql
    for table in ("code_files", "code_chunks", "code_symbols", "code_edges"):
        assert f"FROM loom.{table} FINAL" in sql, table
    assert "FROM loom.code_files\n" not in sql and " FROM loom.code_chunks\n" not in sql


# --- per-tier lag, commits and seconds -------------------------------------


class _Repo:
    def __init__(self, ns: str) -> None:
        self.org, self.namespace, self.git_branch, self.git_dir = "o", ns, "main", "/bare"


def _lag_series(monkeypatch: pytest.MonkeyPatch, rows: list, lags: dict, *, now: float) -> dict:
    from server.code.domain.index import repos as r

    monkeypatch.setattr(r, "configured_repos", lambda: [_Repo("code-loom")])
    out = otel_probes._code_lag_series(
        rows,
        tip_of=lambda d, b: "tip",
        lag_of=lambda d, since, until: lags.get((since, until), (0, 0.0)),
        now=now,
    )
    return {(k, a.get("tier", "")): v for k, xs in out.items() for v, a in xs}


def test_frozen_tier_watermark_while_git_head_moves_reads_as_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Python tier's watermark stopped while the fast tier moved on: the
    oldest commit it has not reached is eight days old and 551 commits back.
    The C++ tier on the same repository is current and reads 0."""
    now = 1_000_000.0
    rows = [
        ("o", "code-loom", "state:git_head", 0.0, "h"),
        ("o", "code-loom", "state:scip_commit", 0.0, "h"),
        ("o", "code-loom", "state:scip_commit_py", 0.0, "py"),
    ]
    lags = {("py", "h"): (551, now - 8 * 86400), ("h", "tip"): (0, 0.0)}
    by = _lag_series(monkeypatch, rows, lags, now=now)
    assert by[("code_scip_lag_commits", "python")] == 551.0
    assert by[("code_tier_lag_seconds", "python")] == 8 * 86400
    assert by[("code_tier_lag_seconds", "python")] > 30 * 60  # the alert threshold
    assert by[("code_scip_lag_commits", "clang")] == 0.0
    assert by[("code_tier_lag_seconds", "clang")] == 0.0
    assert by[("code_tier_lag_seconds", "fast")] == 0.0
    # no watermark, no series: a tier the repository does not run stays silent
    assert ("code_scip_lag_commits", "typescript") not in by


def test_caught_up_tiers_read_zero_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ("o", "code-loom", "state:git_head", 0.0, "h"),
        ("o", "code-loom", "state:scip_commit_ts", 0.0, "h"),
    ]
    by = _lag_series(monkeypatch, rows, {}, now=5_000.0)
    assert by[("code_tier_lag_seconds", "typescript")] == 0.0
    assert by[("code_tier_lag_seconds", "fast")] == 0.0
    assert by[("code_index_lag_commits", "")] == 0.0  # the fast series keeps its attributes


def test_fresh_lag_under_the_threshold_and_unknown_lag_unobserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10_000.0
    rows = [
        ("o", "code-loom", "state:git_head", 0.0, "h"),
        ("o", "code-loom", "state:scip_commit", 0.0, "c"),
        ("o", "code-loom", "state:scip_commit_go", 0.0, "gone"),
    ]
    lags = {("c", "h"): (3, now - 600), ("gone", "h"): (-1, 0.0), ("h", "tip"): (2, now - 60)}
    by = _lag_series(monkeypatch, rows, lags, now=now)
    assert by[("code_tier_lag_seconds", "clang")] == 600.0
    assert by[("code_tier_lag_seconds", "fast")] == 60.0
    assert ("code_tier_lag_seconds", "go") not in by


def test_git_lag_reads_count_and_oldest_commit_time(tmp_path: Any) -> None:
    import subprocess

    def git(*args: str, **env: str) -> str:
        import os

        e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", **env}
        e.update({"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True, env=e
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    for i, ts in enumerate(("1000000000", "1000000100", "1000000200")):
        git("commit", "-q", "--allow-empty", "-m", f"c{i}", GIT_COMMITTER_DATE=f"@{ts} +0000")
    first = git("rev-list", "--max-parents=0", "HEAD")
    head = git("rev-parse", "HEAD")
    assert otel_probes._git_lag(str(tmp_path), first, head) == (2, 1000000100.0)
    assert otel_probes._git_lag(str(tmp_path), head, head) == (0, 0.0)
    assert otel_probes._git_lag(str(tmp_path), "nope", head) == (-1, 0.0)


def test_tier_lag_seconds_gauge_registered() -> None:
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict)
    assert "loom.code.tier_lag_seconds" in meter.gauges
    assert meter.descriptions["loom.code.tier_lag_seconds"]


def test_git_lag_runs_git_only_on_object_ids(monkeypatch: Any) -> None:
    """A compiler-tier watermark is a writer-supplied `commit_sha`: one that
    is not an object id never reaches `git log`, where `--output=<path>`
    would write a file."""
    import subprocess

    calls: list[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    head = "a" * 40
    for bad in ("--output=/tmp/x", "-p", "HEAD", "a" * 6, "g" * 40, ""):
        assert otel_probes._git_lag("/nonexistent", bad, head) == (-1, 0.0), bad
        assert otel_probes._git_lag("/nonexistent", head, bad) == (-1, 0.0), bad
    assert calls == []
