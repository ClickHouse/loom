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
    assert recorder._published_lag(_state(), tier) == lag


def test_a_fetch_since_the_snapshot_keeps_its_lag_as_a_lower_bound() -> None:
    # the tip moved on (it only advances); the watermark did not: still 413 or more behind
    assert recorder._published_lag(_state(tip="d" * 40), "clang") == 413


def test_a_watermark_moved_since_the_snapshot_gives_no_lag() -> None:
    state = _state()
    state["scip_commit"] = {"value": "e" * 40}
    assert recorder._published_lag(state, "clang") == -1


class _Cfg:
    def __init__(self, ns: str) -> None:
        self.org, self.namespace, self.repo, self.git_branch = "o", ns, "o/r", "main"


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
    monkeypatch.setattr("server.code.app.index.repo_registry.registry_read", lambda: True)
    assert recorder.problem_rows() is None  # not read yet: unknown, not healthy
    await recorder.refresh_failing()
    snap = recorder._FAILING
    assert snap is not None and [
        (f.namespace, f.health.tier, f.lag_commits, f.repo, f.branch, f.restricted) for f in snap
    ] == [("code-a", "clang", 413, "o/r", "main", False)]
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


def test_the_problem_names_the_tier_reason_since_lag_and_fix_but_no_entry() -> None:
    row = {
        "org": "o",
        "namespace": "code-a",
        "tier": "clang",
        "kind": "credential",
        "reason": "no credential",
        "since": "2026-01-01T12:00:00+00:00",
        "failed_passes": 12,
        "lag_commits": 413,
        "fix": "No credential with access (github.com/o/private): set LOOM_CODE_GITHUB_TOKEN.",
        "severity": "error",
    }
    (p,) = _problem((row,))
    assert p.severity == "error" and p.source == "workers"
    assert "clang: no credential, 12 failed passes since 2026-01-01T12:00:00+00:00" in p.message
    assert "at least 413 commits behind" in p.message and "code-a" not in p.message
    assert p.fix == row["fix"]


def test_a_problem_row_carries_the_kinds_fix_without_the_repository() -> None:
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", _failing(4), 413, "o/r", "main", False),
    )
    (row,) = recorder.problem_rows(T0 + timedelta(hours=3)) or ()
    assert row["fix"] == tier_health.fix_for("credential", "")
    assert "private" not in row["fix"] and "github.com" not in row["fix"]


def test_many_failing_tiers_are_one_problem_with_each_fix_once() -> None:
    base = {
        "org": "o",
        "tier": "fetch",
        "kind": "network",
        "reason": "host unreachable",
        "since": "s",
        "failed_passes": 3,
    }
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

    read = {"done": False}  # not read yet in this doctor process

    async def registry(**_: Any) -> list[Any]:
        calls.append("registry")
        read["done"] = True
        return []

    async def refresh(ch: Any = None) -> None:
        calls.append("tiers")
        recorder._FAILING = (
            recorder.FailingTier("o", "code-a", _failing(4), 413, "o/r", "main", False),
        )

    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(repo_registry, "refresh_db_repos", registry)
    monkeypatch.setattr(repo_registry, "registry_read", lambda: read["done"])
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
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", _failing(4), 413, "o/r", "main", False),
    )
    await recorder.refresh_failing()
    assert recorder.problem_rows() == ()


@pytest.mark.doctor_tier_read
@pytest.mark.parametrize("why", ["intel_off", "no_host"])
def test_the_doctor_skips_the_tier_read_when_nothing_could_answer(
    why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli import doctor

    monkeypatch.setattr(settings, "code_intel_enabled", why != "intel_off")
    monkeypatch.setattr(settings, "ch_host", "" if why == "no_host" else "localhost")

    def never() -> Any:
        raise AssertionError("no client is made")

    monkeypatch.setattr("server.core.ch.get_client", never)
    assert doctor._doctor_tier_failures() is None


def test_a_failing_tier_never_makes_the_deployment_degraded() -> None:
    from server.observability.problems import Problem, derive_status

    tier = Problem("code_tier_failing", "error", "m", "f", source="workers")
    assert derive_status(True, [tier]) == "ok"
    assert derive_status(True, [tier, Problem("jwt_secret_empty", "error", "m", "f")]) == "degraded"


def test_problem_rows_put_the_errors_first_and_never_order_by_entry() -> None:
    recorder._FAILING = (
        recorder.FailingTier(
            "o", "code-a", _failing(4), 5, "o/r", "main", False
        ),  # warn: not far behind
        recorder.FailingTier("o", "code-b", _failing(4), 413, "o/r", "main", False),  # error
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
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(repo_registry, "refresh_db_repos", registry)
    monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
    monkeypatch.setattr(recorder, "refresh_failing", refresh)
    assert doctor._doctor_tier_failures() is None


def test_a_refused_ssh_key_gets_the_ssh_fix_without_the_repository() -> None:
    error = "fetch git@github.com:o/private.git: git@github.com: Permission denied (publickey)."
    health = None
    for _ in range(4):
        health = tier_health.failed(health, "fetch", error, T0)
    assert health is not None
    recorder._FAILING = (recorder.FailingTier("o", "code-a", health, -1, "o/r", "main", False),)
    (row,) = recorder.problem_rows(T0) or ()
    assert row["fix"].startswith("The ssh key was refused:") and "private" not in row["fix"]


@pytest.mark.asyncio
async def test_before_the_registry_is_read_the_snapshot_stays_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import repo_registry

    store = _Store([_row("code-a", "clang", _failing(4))], {("o", "code-a"): _state()})
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda ch=None: store)
    monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
    await recorder.refresh_failing()
    assert recorder.problem_rows() is None  # unknown, not healthy


def test_the_deployments_own_errors_list_before_a_failing_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "")
    row = {
        "org": "o",
        "namespace": "code-a",
        "tier": "clang",
        "kind": "credential",
        "reason": "no credential",
        "since": "s",
        "failed_passes": 4,
        "lag_commits": 413,
        "fix": "f",
        "severity": "error",
    }
    errors = [
        p.code
        for p in collect_problems(settings, HealthState(code_tier_failures=(row,)))
        if p.severity == "error"
    ]
    assert errors.index("jwt_secret_empty") < errors.index("code_tier_failing")


@pytest.mark.parametrize(
    ("overrides", "want"),
    [
        ({}, None),  # 4 passes over 2 hours, under 10 passes and 3 hours: transient
        ({"code_tier_failing_warn_passes": 4}, "error"),  # 413 behind, past 100
        ({"code_tier_failing_warn_hours": 1.0}, "error"),
        ({"code_tier_failing_warn_passes": 4, "code_tier_failing_error_lag_commits": 500}, "warn"),
    ],
)
def test_problem_rows_grade_with_the_live_settings(
    overrides: dict[str, float], want: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = {
        "code_tier_failing_warn_passes": 10,
        "code_tier_failing_warn_hours": 3.0,
        "code_tier_failing_error_lag_commits": 100,
    }
    for name, value in {**base, **overrides}.items():
        monkeypatch.setattr(settings, name, value)
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", _failing(4), 413, "o/r", "main", False),
    )
    rows = recorder.problem_rows(T0 + timedelta(hours=2)) or ()
    assert (rows[0]["severity"] if rows else None) == want


LOCKED = (
    "ToolOutputError: git checkout failed: fatal: Unable to create "
    "'/srv/repos/checkout-a/.git/index.lock': File exists.\n\n"
    "Another git process seems to be running in this repository"
)


def _failing_with(error: str, passes: int = 4) -> tier_health.TierHealth:
    health = None
    for i in range(passes):
        health = tier_health.failed(health, "clang_full", error, T0 + timedelta(minutes=30 * i))
    assert health is not None
    return health


def test_a_held_git_lock_is_its_own_kind_and_its_fix_says_what_to_do() -> None:
    assert tier_health.classify(LOCKED) == "git_lock"
    fix = tier_health.fix_for("git_lock", LOCKED, name_where=False)
    assert "index.lock" in fix and "/srv" not in fix


def _two_entries() -> tuple[dict[str, Any], ...]:
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", _failing_with(LOCKED), 0, "o/a", "main", False),
        recorder.FailingTier(
            "o", "code-a-26-4", _failing_with("PermissionError"), -1, "o/a", "26.4", False
        ),
    )
    return recorder.problem_rows(T0 + timedelta(hours=3)) or ()


def _tier_problem(rows: tuple[dict[str, Any], ...], *, named: bool) -> Any:
    (p,) = [
        p
        for p in collect_problems(
            settings,
            HealthState(code_tier_failures=rows, code_tier_names="all" if named else "none"),
        )
        if p.code == "code_tier_failing"
    ]
    return p


def test_an_operator_reads_each_entry_by_repository_branch_and_namespace_with_its_reason() -> None:
    p = _tier_problem(_two_entries(), named=True)
    assert (
        "o/a@main (o/code-a) clang_full: git lock held, 4 failed passes since "
        '2026-01-01T12:00:00+00:00 — "ToolOutputError: git checkout failed: fatal: Unable to '
        "create '/srv/repos/checkout-a/.git/index.lock': File exists.\""
    ) in p.message
    assert (
        "o/a@26.4 (o/code-a-26-4) clang_full: unclassified error, 4 failed passes since "
        '2026-01-01T12:00:00+00:00 — "PermissionError"'
    ) in p.message
    assert "unknown" not in p.message and "commits behind" not in p.message
    assert tier_health.fix_for("git_lock", "", name_where=False) in p.fix
    assert tier_health.fix_for("unknown", "", name_where=False) in p.fix


def test_an_anonymous_caller_reads_each_reason_and_fix_but_no_entry() -> None:
    p = _tier_problem(_two_entries(), named=False)
    assert "clang_full: git lock held, 4 failed passes since" in p.message
    assert "clang_full: unclassified error, 4 failed passes since" in p.message
    assert not any(s in p.message for s in ("o/a", "code-a", "/srv", "PermissionError", "unknown"))
    assert "commits behind" not in p.message
    assert tier_health.fix_for("git_lock", "", name_where=False) in p.fix


def test_a_lag_behind_is_shown_and_a_lag_of_zero_is_not() -> None:
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", _failing_with(LOCKED), 413, "o/a", "main", False),
        recorder.FailingTier("o", "code-b", _failing_with(LOCKED), 0, "o/b", "main", False),
    )
    p = _tier_problem(recorder.problem_rows(T0 + timedelta(hours=3)) or (), named=True)
    assert "at least 413 commits behind" in p.message
    assert p.message.count("commits behind") == 1 and "0 commits" not in p.message


def test_the_error_line_is_the_one_that_says_why() -> None:
    cmake = "x" * 700 + "\nCMake Error at CMakeLists.txt:12\n>> cmake configure failed\nexit 1"
    health = _failing_with(cmake)
    # the marker line repeats the reason: the line above it says which
    assert tier_health.error_line(health.kind, health.error) == "CMake Error at CMakeLists.txt:12"
    # cut, with no line of its kind: the last whole line, where the error sits
    health = _failing_with("y" * 700 + "\n  File x.py, line 3\nValueError: broke")
    assert tier_health.error_line(health.kind, health.error) == "ValueError: broke"
    # cut inside the line its kind matched: that line still names the cause
    health = _failing_with("z" * 700 + " Unable to create '/c/.git/index.lock': File exists.")
    assert health.kind == "git_lock"
    assert tier_health.error_line(health.kind, health.error).endswith("File exists.")
    # one cut line and no kind: that line, not nothing
    health = _failing_with("w" * 900)
    assert tier_health.error_line(health.kind, health.error) == "…" + "w" * 199
    # not cut either: the cause is printed last
    out = "ToolOutputError: git submodule update failed: Submodule 'x' registered\nfatal: boom"
    assert tier_health.error_line("unknown", out) == "fatal: boom"


def test_the_dashboard_says_each_kind_as_the_server_does() -> None:
    import re
    from pathlib import Path

    ts = (Path(__file__).resolve().parents[2] / "frontend/src/lib/tierHealth.ts").read_text()
    block = ts[ts.index("const KIND_WORD") : ts.index("};", ts.index("const KIND_WORD"))]
    words = dict(re.findall(r'^\s+(\w+): "([^"]+)",$', block, re.MULTILINE))
    assert words.pop("unknown") == tier_health.reason("unknown")
    assert words == tier_health.REASONS


def test_a_platform_admin_reads_a_restricted_entry_by_its_reason_alone() -> None:
    recorder._FAILING = (
        recorder.FailingTier("o", "code-secret", _failing_with(LOCKED), 0, "o/s", "main", True),
        recorder.FailingTier("o", "code-a", _failing_with(LOCKED), 0, "o/a", "main", False),
    )
    rows = recorder.problem_rows(T0 + timedelta(hours=3)) or ()
    (p,) = [
        p
        for p in collect_problems(
            settings, HealthState(code_tier_failures=rows, code_tier_names="unrestricted")
        )
        if p.code == "code_tier_failing"
    ]
    assert "a restricted entry's clang_full: git lock held, 4 failed passes" in p.message
    assert "o/a@main (o/code-a)" in p.message
    assert not any(s in p.message for s in ("o/s", "code-secret"))
    assert p.message.count("/srv/repos/checkout-a") == 1  # code-a's error only
    # the host's own view names it
    (d,) = [
        p
        for p in collect_problems(
            settings, HealthState(code_tier_failures=rows, code_tier_names="all")
        )
        if p.code == "code_tier_failing"
    ]
    assert "o/s@main (o/code-secret)" in d.message


@pytest.mark.asyncio
async def test_an_entry_whose_classification_cannot_be_read_counts_as_restricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.directory import ClassificationUnavailable

    async def unreadable(org: str, ns: str) -> bool:
        raise ClassificationUnavailable("directory down")

    monkeypatch.setattr("server.identity.app.namespace_classification.is_restricted", unreadable)
    assert await recorder._restricted("o", "code-a") is True


def test_a_restriction_made_since_the_snapshot_hides_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder._FAILING = (
        recorder.FailingTier("o", "code-a", _failing_with(LOCKED), 0, "o/a", "main", False),
    )
    monkeypatch.setattr(
        "server.identity.app.namespace_classification.is_restricted_cached",
        lambda org, ns: ns == "code-a",
    )
    (row,) = recorder.problem_rows(T0 + timedelta(hours=3)) or ()
    assert row["restricted"] is True


def test_cmakes_error_line_is_the_one_that_says_what_failed() -> None:
    out = (
        "-- Configuring incomplete\n"
        "CMake Error at cmake/tools.cmake:30 (message):\n"
        "  Compiler GNU is not supported\n"
        "Call Stack (most recent call first):\n"
        ">> cmake configure failed"
    )
    assert tier_health.error_line("cmake", out) == "Compiler GNU is not supported"
    no_reason = "CMake Error at CMakeLists.txt:1 (project):\nCall Stack (most recent call first):\n"
    assert tier_health.error_line("cmake", no_reason + ">> cmake configure failed") == (
        "CMake Error at CMakeLists.txt:1 (project):"
    )
    assert tier_health.error_line("cmake", "clang: not found\n>> cmake configure failed") == (
        "clang: not found"
    )


def test_a_delta_over_its_cap_is_recorded_with_its_sizes_and_the_setting_to_raise() -> None:
    from server.code.app.index.untrusted_files import FileRefused

    refused = FileRefused("/w/scip_full_x/delta.json is 545908635 bytes; max 536870912")
    text = recorder.error_text(refused)
    assert text == "FileRefused: /w/scip_full_x/delta.json is 545908635 bytes; max 536870912"
    health = _failing_with(text)
    assert health.kind == "file_refused" and tier_health.reason("file_refused") == "file refused"
    assert "LOOM_CODE_SCIP_DELTA_FILE_MAX_MB (MiB)" in health.fix
    nodes = FileRefused("/w/d.json holds 9 `{`/`[`/`,` bytes; max 8 (code_scip_delta_max_nodes)")
    nodes_health = _failing_with(recorder.error_text(nodes))
    assert nodes_health.kind == "file_refused"
    assert nodes_health.fix.startswith("The compiler tier's delta holds more JSON values")
    # a checkout's own file over its fixed cap: nothing to raise
    for name in ("package.json", "yarn.lock"):
        manifest = FileRefused(f"/r/co/{name} is 99999999 bytes; max 1048576")
        fix = _failing_with(recorder.error_text(manifest)).fix
        assert fix.startswith("A file of the checkout") and "LOOM_" not in fix, name
    assert health.fix != fix  # the delta's fix is its own


def test_a_go_module_that_needs_a_private_module_says_so() -> None:
    error = (
        "TierDegraded: services/api: go mod download failed: If this is a private "
        "repository, see https://go.dev/doc/faq#git_https for additional information."
    )
    health = _failing_with(error)
    assert health.kind == "module"
    assert "Where it says a private repository" in health.fix
    assert "without a credential" in health.fix


def test_a_linked_or_changed_file_reads_refused_with_its_own_fix() -> None:
    from server.code.app.index.untrusted_files import FileRefused

    for text in (
        "/w/delta.json is a symlink or a symlink loop; not read",
        "/r/co/package.json is not a regular file; not read",
        "/w/delta.json changed while it was read",
    ):
        health = _failing_with(recorder.error_text(FileRefused(text)))
        assert health.kind == "file_refused", text
        assert health.fix.startswith("The tier refused a file it reads"), text


def test_a_harness_failure_quotes_the_tools_last_line() -> None:
    record = (
        "ToolOutputError: harness exit 1: Traceback (most recent call last):\n"
        '  File "convert.py", line 3, in main\n'
        "ValueError: bad delta"
    )
    assert tier_health.classify(record) == "harness"
    assert tier_health.error_line("harness", record) == "ValueError: bad delta"


def test_restricted_entries_come_after_the_named_ones() -> None:
    recorder._FAILING = (
        recorder.FailingTier("a", "code-a", _failing_with(LOCKED), 0, "a/a", "main", True),
        recorder.FailingTier("b", "code-b", _failing_with(LOCKED), 0, "b/b", "main", False),
    )
    rows = recorder.problem_rows(T0 + timedelta(hours=3)) or ()
    (p,) = [
        p
        for p in collect_problems(
            settings, HealthState(code_tier_failures=rows, code_tier_names="unrestricted")
        )
        if p.code == "code_tier_failing"
    ]
    assert p.message.index("b/b@main") < p.message.index("a restricted entry's")


def test_the_anonymous_order_does_not_place_a_restricted_entry() -> None:
    """Three entries alike but for their names: the anonymous answer lists them in an
    order that does not follow org or namespace, so it cannot be matched against the
    named view to bound where the restricted one sits."""

    def snapshot(names: list[str]) -> tuple[dict[str, Any], ...]:
        recorder._FAILING = tuple(
            recorder.FailingTier(
                "o", ns, _failing_with(LOCKED, passes), 0, f"o/{ns}", "main", ns == "code-b"
            )
            for ns, passes in zip(names, (4, 5, 6), strict=True)
        )
        return recorder.problem_rows(T0 + timedelta(hours=3)) or ()

    def message(rows: Any, names: str) -> str:
        (p,) = [
            p
            for p in collect_problems(
                settings, HealthState(code_tier_failures=rows, code_tier_names=names)
            )
            if p.code == "code_tier_failing"
        ]
        return p.message

    # the same lines with the names swapped read the same anonymously
    a = message(snapshot(["code-a", "code-b", "code-c"]), "none")
    b = message(snapshot(["code-c", "code-b", "code-a"]), "none")
    assert a == b
    admin = message(snapshot(["code-a", "code-b", "code-c"]), "unrestricted")
    assert admin.index("o/code-a@") < admin.index("o/code-c@") < admin.index("a restricted entry's")


def test_rows_that_tie_but_read_differently_are_not_left_in_entry_order() -> None:
    no_token = _failing_with(NO_TOKEN)
    locked = _failing_with(LOCKED)
    assert no_token.failing_since == locked.failing_since  # a tie on tier, since, passes
    orders = []
    for middle in (no_token, locked):
        recorder._FAILING = tuple(
            recorder.FailingTier("o", ns, h, 0, f"o/{ns}", "main", ns == "code-b")
            for ns, h in (
                ("code-a", locked if middle is no_token else no_token),
                ("code-b", middle),
                ("code-c", locked if middle is no_token else no_token),
            )
        )
        rows = recorder.problem_rows(T0 + timedelta(hours=3)) or ()
        orders.append([r["reason"] for r in rows])
    # sorted by what the lines say: the restricted one's place follows its reason only
    assert all(order == sorted(order) for order in orders)
