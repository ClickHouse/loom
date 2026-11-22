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
        assert state["cases"]["nightly-head"] == {}
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
