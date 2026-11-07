"""A code-index tier that keeps failing is one `code_tier_failing` health problem, fed
from a per-process snapshot of the tier-health records (never a ClickHouse read in the
probe): warn past the pass or hour threshold, error when also far behind, with the
tier, kind, since, lag and fix."""

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


@pytest.fixture(autouse=True)
def _fresh_snapshot() -> Any:
    """The snapshot and the doctor's cached read are process-wide: each test starts and
    leaves both unread."""
    from server.cli import doctor

    recorder.reset_for_tests()
    getattr(doctor._doctor_tier_failures, "cache_clear", lambda: None)()
    yield
    recorder.reset_for_tests()
    getattr(doctor._doctor_tier_failures, "cache_clear", lambda: None)()


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


TIP, HEAD, SCIP = "c" * 40, "b" * 40, "a" * 40


def _state(*, tip: str = TIP) -> dict[str, dict[str, Any]]:
    """An entry's state with a status snapshot published against `TIP`."""
    snap = {
        "clone_head": TIP,
        "git_head": HEAD,
        "fast_tier_lag": 7,
        "compiler_tiers": [{"watermark_key": "scip_commit", "commit": SCIP, "lag": 413}],
    }
    return {
        "clone_head": {"value": tip, "detail": "main"},
        "git_head": {"value": HEAD},
        "scip_commit": {"value": SCIP},
        "status_snapshot": {"value": json.dumps(snap)},
    }


@pytest.mark.parametrize(
    ("tier", "lag"),
    [("ingest", 7), ("clang", 413), ("clang_full", 413), ("python", -1), ("fetch", -1)],
)
def test_the_lag_is_the_one_the_published_status_counts(tier: str, lag: int) -> None:
    assert recorder._published_lag(_state(), "main", tier) == lag


def test_a_snapshot_from_another_tip_gives_no_lag() -> None:
    assert recorder._published_lag(_state(tip="d" * 40), "main", "clang") == -1


class _Cfg:
    def __init__(self, ns: str) -> None:
        self.org, self.namespace, self.git_branch = "o", ns, "main"


class _Store:
    def __init__(self, rows: list[dict[str, Any]], states: dict[tuple[str, str], Any]) -> None:
        self.rows, self.states = rows, states
        self.fail = False

    async def state_rows_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("clickhouse down")
        return [r for r in self.rows if str(r["state_key"]).startswith(prefix)]

    async def all_state_many(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], Any]:
        return {p: self.states.get(p, {}) for p in pairs}


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
            _row("code-gone", "clang", _failing(4)),  # no longer configured: nothing runs it
        ],
        {("o", "code-a"): _state()},
    )
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda ch=None: store)
    monkeypatch.setattr(
        "server.code.domain.index.repos.configured_repos",
        lambda: [_Cfg("code-a"), _Cfg("code-b")],
    )
    assert recorder.problem_rows() is None  # not read yet: unknown, not healthy
    await recorder.refresh_failing()
    snap = recorder._FAILING
    assert snap is not None and [(f.namespace, f.health.tier, f.lag_commits) for f in snap] == [
        ("code-a", "clang", 413)
    ]
    store.fail = True
    await recorder.refresh_failing()
    assert recorder._FAILING == snap
    rows = recorder.problem_rows(T0 + timedelta(hours=2))
    assert rows is not None and rows[0]["severity"] == "error" and rows[0]["kind"] == "credential"


def _problem(rows: tuple[dict[str, Any], ...] | None) -> list[Any]:
    return [
        p
        for p in collect_problems(settings, HealthState(code_tier_failures=rows))
        if p.code == "code_tier_failing"
    ]


def test_the_problem_names_the_tier_kind_since_lag_and_fix_but_no_entry() -> None:
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
    assert "clang (credential, 12 failed passes since 2026-01-01T12:00:00+00:00" in p.message
    assert "413 commits behind" in p.message and "code-a" not in p.message
    assert p.fix == row["fix"]


def test_a_problem_row_carries_the_kinds_fix_without_the_repository() -> None:
    recorder._FAILING = (recorder.FailingTier("o", "code-a", _failing(4), 413),)
    (row,) = recorder.problem_rows(T0 + timedelta(hours=3)) or ()
    assert row["fix"] == tier_health.fix_for("credential", "")
    assert "private" not in row["fix"] and "github.com" not in row["fix"]


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


class _Client:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.connected = fail, False

    async def connect(self) -> None:
        if self.fail:
            raise OSError("clickhouse down")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


@pytest.mark.doctor_tier_read
@pytest.mark.parametrize("down", [False, True])
def test_the_doctor_reads_the_failing_tiers_once_and_none_when_clickhouse_is_down(
    down: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli import doctor
    from server.code.app.index import repo_registry

    client = _Client(fail=down)
    calls: list[str] = []
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "ch_host", "localhost")
    monkeypatch.setattr(doctor, "_doctor_ch_reachable", lambda *_: (True, "ok"))

    async def registry(**_: Any) -> list[Any]:
        calls.append("registry")
        return []

    async def refresh(ch: Any = None) -> None:
        calls.append("tiers")
        recorder._FAILING = (recorder.FailingTier("o", "code-a", _failing(4), 413),)

    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(repo_registry, "refresh_db_repos", registry)
    monkeypatch.setattr(repo_registry, "registry_read", lambda: True)
    monkeypatch.setattr(recorder, "refresh_failing", refresh)

    async def overrides(**_: Any) -> None:
        calls.append("overrides")

    monkeypatch.setattr("server.core.settings_overrides.refresh", overrides)
    rows = doctor._doctor_tier_failures()
    if down:
        assert rows is None and calls == []
    else:
        assert rows is not None and [r["namespace"] for r in rows] == ["code-a"]
        assert calls == ["registry", "overrides", "tiers"] and not client.connected
        assert doctor._doctor_tier_failures() == rows  # `--json` asks twice: one read
        assert calls == ["registry", "overrides", "tiers"]


@pytest.mark.asyncio
async def test_the_registry_refresher_re_reads_the_failing_tiers_at_start_and_each_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from server.code.app.index import repo_registry

    reads: list[int] = []

    async def registry(**_: Any) -> list[Any]:
        return []

    async def refresh(ch: Any = None) -> None:
        reads.append(1)

    monkeypatch.setattr(repo_registry, "refresh_db_repos", registry)
    monkeypatch.setattr(recorder, "refresh_failing", refresh)
    monkeypatch.setattr(repo_registry.settings, "code_repos_refresh_seconds", 0)
    task = await repo_registry.start_refresher()
    assert reads == [1]  # before the first request is served
    for _ in range(100):
        if len(reads) >= 2:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(reads) >= 2


@pytest.mark.asyncio
async def test_with_code_intelligence_off_no_record_is_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", False)
    recorder._FAILING = (recorder.FailingTier("o", "code-a", _failing(4), 413),)
    await recorder.refresh_failing()
    assert recorder.problem_rows() == ()


@pytest.mark.doctor_tier_read
@pytest.mark.parametrize("why", ["intel_off", "unreachable"])
def test_the_doctor_skips_the_tier_read_when_nothing_could_answer(
    why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli import doctor

    monkeypatch.setattr(settings, "code_intel_enabled", why != "intel_off")
    monkeypatch.setattr(settings, "ch_host", "localhost")
    monkeypatch.setattr(doctor, "_doctor_ch_reachable", lambda *_: (False, "refused"))

    def never() -> Any:
        raise AssertionError("no client is made")

    monkeypatch.setattr("server.core.ch.get_client", never)
    assert doctor._doctor_tier_failures() is None


def test_a_failing_tier_never_makes_the_deployment_degraded() -> None:
    from server.observability.problems import Problem, derive_status

    tier = Problem("code_tier_failing", "error", "m", "f", source="workers")
    assert derive_status(True, [tier]) == "ok"
    assert derive_status(True, [tier, Problem("jwt_secret_empty", "error", "m", "f")]) == "degraded"


def test_problem_rows_put_the_errors_first() -> None:
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", _failing(4), 5),  # warn: not far behind
        recorder.FailingTier("o", "code-b", _failing(4), 413),  # error
    )
    rows = recorder.problem_rows(T0 + timedelta(hours=3)) or ()
    assert [(r["namespace"], r["severity"]) for r in rows] == [
        ("code-b", "error"),
        ("code-a", "warn"),
    ]


def test_the_doctor_grades_a_failing_tier_at_most_warn() -> None:
    from server.cli import doctor
    from server.observability.problems import Problem

    tier = Problem("code_tier_failing", "error", "m", "f", source="workers")
    other = Problem("jwt_secret_empty", "error", "m", "f")
    grades = {name: g for name, g, _ in doctor._problem_checks([tier, other])}
    assert grades == {
        "problem:code_tier_failing": doctor.WARN,
        "problem:jwt_secret_empty": doctor.FAIL,
    }


@pytest.mark.doctor_tier_read
def test_the_doctor_says_nothing_when_the_registry_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import doctor
    from server.code.app.index import repo_registry

    async def registry(**_: Any) -> list[Any]:
        return []

    async def refresh(ch: Any = None) -> None:
        raise AssertionError("no tier read without the configured entries")

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "ch_host", "localhost")
    monkeypatch.setattr(doctor, "_doctor_ch_reachable", lambda *_: (True, "ok"))
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(repo_registry, "refresh_db_repos", registry)
    monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
    monkeypatch.setattr(recorder, "refresh_failing", refresh)
    assert doctor._doctor_tier_failures() is None


def test_a_refused_ssh_key_gets_the_ssh_fix_without_the_repository() -> None:
    error = "fetch git@github.com:o/private.git: git@github.com: Permission denied (publickey)."
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", tier_health.failed(None, "fetch", error, T0), -1),
    )
    for _ in range(3):
        health = recorder._FAILING[0].health
        recorder._FAILING = (
            recorder.FailingTier("o", "code-a", tier_health.failed(health, "fetch", error, T0), -1),
        )
    (row,) = recorder.problem_rows(T0) or ()
    assert row["fix"].startswith("The ssh key was refused:") and "private" not in row["fix"]
