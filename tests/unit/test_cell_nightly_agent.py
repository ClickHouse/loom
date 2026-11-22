"""Daily observation windows and unfinished repairs survive catch-up and retries."""

import datetime as dt
from types import SimpleNamespace

import pytest

from dev.cell_nightly_agent import monitor


def test_early_catchup_does_not_consume_the_upcoming_observation_window():
    early = monitor.parse("2030-01-02T04:00:00Z")
    scheduled = monitor.parse("2030-01-02T06:13:00Z")
    assert monitor.window(early) == monitor.parse("2030-01-01T06:13:00Z")
    assert monitor.window(scheduled) == scheduled


@pytest.mark.parametrize("status", ["failed", "timed_out", "blocked_auth", "unfinished", "started"])
def test_an_unfinished_attempt_can_resume_after_backoff(status):
    due = monitor.parse("2030-01-02T06:13:00Z")
    case = dict(status=status, fingerprint="same", attempts=1, next_retry=due.isoformat())
    assert monitor.plan(case, "same", due) == "resume"
    assert monitor.plan(case, "same", due - dt.timedelta(days=1)) == "backoff"


def test_unchanged_attempts_are_bounded_and_changed_pr_evidence_reopens_work():
    due = monitor.parse("2030-01-02T06:13:00Z")
    case = dict(fingerprint="same", attempts=3)
    assert monitor.plan(case, "same", due).startswith("needs_attention")
    assert monitor.plan(case, "changed-checks-or-pr-head", due) == "repair"


@pytest.mark.parametrize("auth_ok", [True, False])
def test_existing_fix_is_reused_and_preflight_failure_does_not_consume_attempt(
    monkeypatch, tmp_path, auth_ok
):
    now = monitor.parse("2030-01-02T06:13:00Z")
    run = dict(
        id=1,
        event="schedule",
        head_branch="main",
        created_at="2030-01-02T02:44:00Z",
        head_sha="nightly-head",
        html_url="synthetic-only",
        status="completed",
        conclusion="failure",
    )
    pr = dict(number=2, head=dict(sha="fix-head"), html_url="synthetic-only-fix-pr")

    def github(*args):
        path = args[1]
        if "/workflows/" in path:
            return dict(workflow_runs=[run])
        if "/jobs?" in path:
            return dict(jobs=[])
        if "/pulls?" in path:
            return [pr]
        if "/check-runs?" in path:
            return dict(check_runs=[])
        assert path.endswith("/commits/main")
        return dict(sha="main-head")

    started = []
    monkeypatch.setattr(monitor, "TASK_DIR", tmp_path)
    (tmp_path / "TASK.md").write_text("Synthetic-only task; no model calls.")
    monkeypatch.setattr(monitor, "gh", github)
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0 if auth_ok else 1,
            stdout="Logged in using ChatGPT" if auth_ok else "unavailable",
        ),
    )
    monkeypatch.setattr(monitor, "tmux_run", lambda: started.append(True) or dict(status="failed"))
    state = {}
    config = dict(first_expected_nightly="2030-01-01T02:43:00Z")
    result = monitor.check(state, now, config)
    if not auth_ok:
        assert result["disposition"] == "blocked_auth"
        assert "nightly-head" not in state["cases"]
        assert started == []
        return
    assert len(started) == 1
    assert "synthetic-only-fix-pr" in (tmp_path / "current-task.md").read_text()
    assert state["cases"]["nightly-head"]["attempts"] == 1
    assert monitor.check(state, now, config)["disposition"] == "window_already_observed"
    run.update(id=3, created_at="2030-01-03T02:44:00Z")
    monitor.check(state, now + dt.timedelta(days=1), config)
    assert len(started) == 2
    assert state["cases"]["nightly-head"]["attempts"] == 2


def test_existing_codex_auth_and_home_are_preserved_without_ambient_provider_keys(monkeypatch):
    monkeypatch.setenv("HOME", "/synthetic/user")
    monkeypatch.setenv("CODEX_HOME", "/synthetic/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-never-used")
    environment = monitor.repair_environment()
    assert environment["HOME"] == "/synthetic/user"
    assert environment["CODEX_HOME"] == "/synthetic/codex"
    assert "OPENAI_API_KEY" not in environment


def test_catchup_cannot_occupy_the_service_across_the_next_daily_activation(monkeypatch):
    now = monitor.parse("2030-01-02T05:40:00Z")

    def unexpected_api(*args):
        raise AssertionError("Near-boundary catchup must defer before slow preflight or repair")

    monkeypatch.setattr(monitor, "gh", unexpected_api)
    state = {}
    assert monitor.check(state, now, {})["disposition"] == "deferred_until_next_window"
    assert state["next_check"] == "2030-01-02T06:13:00+00:00"
    assert "last_window" not in state


@pytest.mark.parametrize("probe", ["--tmux-probe", "--timeout-probe"])
def test_an_inert_probe_cannot_overwrite_an_active_daily_repair(
    monkeypatch, tmp_path, capsys, probe
):
    import fcntl
    import sys

    monkeypatch.setattr(monitor, "TASK_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--dry-run", probe])
    result = tmp_path / "repair-result.json"
    result.write_text("active-repair-evidence")

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("Probe must not start while the daily monitor owns the lock")

    monkeypatch.setattr(monitor, "tmux_run", unexpected_probe)
    with (tmp_path / "monitor.lock").open("w") as lock:
        lock.write("active-lock-owner")
        lock.flush()
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monitor.main()
    assert "Another daily check owns the lock" in capsys.readouterr().out
    assert result.read_text() == "active-repair-evidence"
    assert (tmp_path / "monitor.lock").read_text() == "active-lock-owner"


@pytest.mark.parametrize("probe", ["--tmux-probe", "--timeout-probe"])
def test_probe_flags_require_inert_mode_before_any_real_observation(monkeypatch, probe):
    import sys

    monkeypatch.setattr(sys, "argv", ["monitor.py", probe])

    def unexpected_observation(*args, **kwargs):
        raise AssertionError("A probe flag must never enter the live repair path")

    monkeypatch.setattr(monitor, "check", unexpected_observation)
    with pytest.raises(SystemExit) as exit_status:
        monitor.main()
    assert exit_status.value.code == 2


def test_scope_migration_must_finish_before_the_pane_can_be_reattached(monkeypatch):
    import json

    answers = [
        json.dumps(
            [
                dict(unit="unrelated.scope", description="another worker"),
                dict(unit="owned.scope", description="tmux child pane 12 launched by process 11"),
            ]
        ),
        "activating\n",
        "active\n",
    ]
    calls = []

    def output(argv, **kwargs):
        calls.append(argv)
        return answers.pop(0)

    monkeypatch.setattr(monitor.subprocess, "check_output", output)
    monkeypatch.setattr(monitor.time, "sleep", lambda seconds: None)
    monitor.settle_pane_scope(11, 12)
    assert answers == []
    assert len(calls) == 3
    assert all(argv[-1] == "owned.scope" for argv in calls[1:])


def test_failed_owned_scope_is_refused_instead_of_starting_repair(monkeypatch):
    import json

    answers = [
        json.dumps(
            [dict(unit="owned.scope", description="tmux child pane 12 launched by process 11")]
        ),
        "failed\n",
    ]
    monkeypatch.setattr(monitor.subprocess, "check_output", lambda *a, **k: answers.pop(0))
    with pytest.raises(RuntimeError, match="did not finish starting"):
        monitor.settle_pane_scope(11, 12)


def test_scope_absence_is_polled_before_scope_activation(monkeypatch):
    import json

    answers = [
        "[]",
        json.dumps(
            [dict(unit="owned.scope", description="tmux child pane 12 launched by process 11")]
        ),
        "activating\n",
        "active\n",
    ]
    monkeypatch.setattr(monitor.subprocess, "check_output", lambda *a, **k: answers.pop(0))
    monkeypatch.setattr(monitor.time, "sleep", lambda seconds: None)
    monitor.settle_pane_scope(11, 12)
    assert answers == []


def test_missing_owned_scope_fails_after_the_bounded_deadline(monkeypatch):
    ticks = iter([0, 6])
    monkeypatch.setattr(monitor.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(monitor.subprocess, "check_output", lambda *a, **k: "[]")
    with pytest.raises(RuntimeError, match="did not appear"):
        monitor.settle_pane_scope(11, 12)
