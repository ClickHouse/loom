"""code_scip_full seeds and re-indexes one due entry per pass; code_crossref
runs once per git_head."""

from __future__ import annotations

import asyncio
import os
import select
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import scip_full as app
from server.code.app.index.scip_delta import ScipApplyStats
from server.code.workers import crossref, scip_full
from server.settings import settings
from tests.unit.code_fixtures import repo_config


@pytest.fixture(autouse=True)
def _lock_dir(monkeypatch, tmp_path):
    from server.settings import settings
    from tests.unit.claims_fake import held_leases

    monkeypatch.setattr(settings, "code_scip_lock_dir", str(tmp_path / "locks"))
    held_leases(monkeypatch)
    monkeypatch.setattr(app.shutil, "disk_usage", lambda path: SimpleNamespace(free=2**60))


def _cfg(tmp_path: Path, ns: str) -> Any:
    (tmp_path / "bare.git").mkdir(exist_ok=True)
    return repo_config(
        org="o",
        namespace=ns,
        scip_producer="clang",
        git_dir=str(tmp_path / "bare.git"),
        scip_checkout_dir=str(tmp_path / f"checkout-{ns}"),
    )


def _repo(state: dict[tuple[str, str], str]) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return state.get((ns, key), "")

    async def set_state(org, ns, key, value, detail=""):
        state[(ns, key)] = value

    repo.get_state.side_effect = get_state
    repo.set_state.side_effect = set_state
    return repo


async def _full_pass(tmp_path, monkeypatch, state, cfgs) -> tuple[Any, AsyncMock]:
    from server.settings import settings

    clang = tmp_path / "scip-clang"
    clang.touch()
    monkeypatch.setattr(settings, "code_scip_clang_path", str(clang))
    run = AsyncMock(return_value=(ScipApplyStats(), ""))
    monkeypatch.setattr(scip_full, "full_index", run)
    with (
        patch("server.code.store.index.CodeRepo", return_value=_repo(state)),
        patch("server.code.domain.index.repos.configured_repos", return_value=cfgs),
    ):
        return await scip_full.run_pass(), run


@pytest.mark.asyncio
async def test_an_unseeded_entry_gets_its_first_index_and_a_checkout(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, "code-a")
    res, run = await _full_pass(tmp_path, monkeypatch, {("code-a", "git_head"): "a" * 40}, [cfg])
    assert res.deltas_applied == 1 and not res.errors
    assert (tmp_path / "checkout-code-a" / ".git").is_dir()
    assert run.await_args.args[5] == "a" * 40


@pytest.mark.asyncio
async def test_one_entry_per_pass(tmp_path, monkeypatch):
    state = {("code-a", "git_head"): "a" * 40, ("code-b", "git_head"): "b" * 40}
    cfgs = [_cfg(tmp_path, "code-a"), _cfg(tmp_path, "code-b")]
    res, run = await _full_pass(tmp_path, monkeypatch, state, cfgs)
    assert res.deltas_applied == 1 and run.await_count == 1


@pytest.mark.asyncio
async def test_a_recent_full_run_is_not_due(tmp_path, monkeypatch):
    now = str(int(time.time()) - 7 * 3600)
    state = {
        ("code-a", "git_head"): "a" * 40,
        ("code-a", "scip_commit"): "a" * 40,
        ("code-a", "scip_full_at"): now,
        ("code-a", "scip_full_done_at"): now,
    }
    res, run = await _full_pass(tmp_path, monkeypatch, state, [_cfg(tmp_path, "code-a")])
    assert run.await_count == 0 and res.deltas_applied == 0


@pytest.mark.asyncio
async def test_a_week_old_full_run_is_due(tmp_path, monkeypatch):
    week = str(int(time.time()) - 8 * 24 * 3600)
    state = {
        ("code-a", "git_head"): "a" * 40,
        ("code-a", "scip_commit"): "a" * 40,
        ("code-a", "scip_full_at"): week,
        ("code-a", "scip_full_done_at"): week,
    }
    _, run = await _full_pass(tmp_path, monkeypatch, state, [_cfg(tmp_path, "code-a")])
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_without_scip_clang_the_entry_is_a_skip(tmp_path, monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_clang_path", str(tmp_path / "absent"))
    run = AsyncMock()
    monkeypatch.setattr(scip_full, "full_index", run)
    with (
        patch(
            "server.code.store.index.CodeRepo",
            return_value=_repo({("code-a", "git_head"): "a" * 40}),
        ),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_cfg(tmp_path, "code-a")],
        ),
    ):
        res = await scip_full.run_pass()
    assert run.await_count == 0 and "scip-clang missing" in res.skipped[0]


@pytest.mark.asyncio
async def test_crossref_runs_once_per_git_head(tmp_path, monkeypatch):
    from server.code.domain.extract import crossref as extract

    state = {("code-a", "git_head"): "a" * 40}
    run = AsyncMock(return_value=extract.CrossrefResult())
    monkeypatch.setattr(extract, "run_crossref_pass", run)
    with (
        patch("server.code.store.index.CodeRepo", return_value=_repo(state)),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_cfg(tmp_path, "code-a")],
        ),
    ):
        first = await crossref.run_pass()
        second = await crossref.run_pass()
    assert first.passes_run == 1 and second.passes_run == 0
    assert state[("code-a", "crossref_commit")] == "a" * 40


@pytest.mark.asyncio
async def test_every_hours_zero_only_seeds(tmp_path, monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_full_every_hours", 0)
    state = {("code-a", "git_head"): "a" * 40, ("code-a", "scip_commit"): "a" * 40}
    _, run = await _full_pass(tmp_path, monkeypatch, state, [_cfg(tmp_path, "code-a")])
    assert run.await_count == 0


@pytest.mark.asyncio
async def test_the_full_run_gets_the_configured_jobs_and_timeout(tmp_path, monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_clang_jobs", 3)
    monkeypatch.setattr(settings, "code_scip_full_timeout_seconds", 1234)
    state = {("code-a", "git_head"): "a" * 40}
    _, run = await _full_pass(tmp_path, monkeypatch, state, [_cfg(tmp_path, "code-a")])
    assert run.await_args.kwargs == {"timeout_s": 1234, "jobs": 3}


@pytest.mark.parametrize(
    ("flag", "worker"),
    [("code_scip_full_enabled", "code_scip_full"), ("code_crossref_enabled", "code_crossref")],
)
def test_the_c_tier_workers_follow_their_flags(monkeypatch, flag: str, worker: str) -> None:
    from server.settings import settings
    from server.workers.registry import enabled_workers

    monkeypatch.setattr(settings, "code_scip_full_interval_seconds", 120)
    monkeypatch.setattr(settings, "code_crossref_interval_seconds", 240)
    monkeypatch.setattr(settings, flag, True)
    specs = {w.name: w for w in enabled_workers()}
    assert specs[worker].interval_seconds == {"code_scip_full": 120, "code_crossref": 240}[worker]
    monkeypatch.setattr(settings, flag, False)
    assert worker not in {w.name for w in enabled_workers()}


@pytest.mark.asyncio
async def test_a_failed_full_run_still_ends_the_pass(tmp_path, monkeypatch):
    """A failure is the pass's one entry too: the next due entry is not started
    with what is left of the deadline."""
    from server.code.app.index.scip_full import FullIndexFailed
    from server.settings import settings

    clang = tmp_path / "scip-clang"
    clang.touch()
    monkeypatch.setattr(settings, "code_scip_clang_path", str(clang))
    run = AsyncMock(side_effect=FullIndexFailed(2, "cmake failed"))
    monkeypatch.setattr(scip_full, "full_index", run)
    state = {("code-a", "git_head"): "a" * 40, ("code-b", "git_head"): "b" * 40}
    with (
        patch("server.code.store.index.CodeRepo", return_value=_repo(state)),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_cfg(tmp_path, "code-a"), _cfg(tmp_path, "code-b")],
        ),
    ):
        res = await scip_full.run_pass()
    assert run.await_count == 1 and len(res.errors) == 1


def test_the_work_dir_is_made_where_the_setting_says(tmp_path, monkeypatch) -> None:
    from server.code.app.index.scip_full import work_dir
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_work_dir", "")
    assert work_dir() is None
    monkeypatch.setattr(settings, "code_scip_work_dir", str(tmp_path / "scip-work"))
    assert work_dir() == str(tmp_path / "scip-work") and (tmp_path / "scip-work").is_dir()


def test_a_crashed_runs_work_dir_is_removed_and_a_live_one_kept(tmp_path, monkeypatch) -> None:
    import os

    from server.code.app.index.scip_full import work_dir
    from server.settings import settings

    root = tmp_path / "scip-work"
    old, live = root / "scip_full_old", root / "scip_delta_live"
    old.mkdir(parents=True)
    live.mkdir()
    day_ago = time.time() - 25 * 3600
    os.utime(old, (day_ago, day_ago))
    monkeypatch.setattr(settings, "code_scip_work_dir", str(root))
    work_dir()
    assert not old.exists() and live.exists()


@pytest.mark.asyncio
async def test_a_killed_weekly_run_is_retried_after_six_hours_not_a_week(tmp_path, monkeypatch):
    """The start is stamped and the pod is replaced mid-run: no completion was
    recorded, so the entry is due again once the retry window has passed."""
    hour = 3600
    week_ago = str(int(time.time()) - 8 * 24 * hour)
    state = {
        ("code-a", "git_head"): "a" * 40,
        ("code-a", "scip_commit"): "a" * 40,
        ("code-a", "scip_full_done_at"): week_ago,
        ("code-a", "scip_full_at"): str(int(time.time()) - hour),
    }
    cfgs = [_cfg(tmp_path, "code-a")]
    _, run = await _full_pass(tmp_path, monkeypatch, state, cfgs)
    assert run.await_count == 0
    state[("code-a", "scip_full_at")] = str(int(time.time()) - 7 * hour)
    _, run = await _full_pass(tmp_path, monkeypatch, state, cfgs)
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_run_restarts_the_retry_window_at_its_end(tmp_path, monkeypatch) -> None:
    """A run that fails at its timeout hours after it started is not due again
    on the next pass."""
    from server.code.app.index import scip_full as app

    state: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(app, "move_checkout", AsyncMock())
    monkeypatch.setattr(app, "run_full_harness", AsyncMock(return_value=(124, "timed out")))
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_work_dir", "")
    ticks = [1000.0, 1000.0 + 8 * 3600]
    monkeypatch.setattr(app.time, "time", lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0])
    with pytest.raises(app.FullIndexFailed):
        await app.full_index(
            _repo(state),
            _cfg(tmp_path, "code-a"),
            tmp_path,
            tmp_path,
            tmp_path,
            "a" * 40,
            timeout_s=1,
        )
    assert state[("code-a", "scip_full_at")] == str(int(1000 + 8 * 3600))
    assert ("code-a", "scip_full_done_at") not in state


def test_harness_tools_write_their_temp_files_beside_the_delta(tmp_path) -> None:
    import subprocess
    import sys

    from server.core.paths import SCIP_HARNESS_DIR

    out = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; echo "$TMPDIR"; "$2" -c "import tempfile; print(tempfile.mkstemp()[1])"',
            "harness-temp",
            str(SCIP_HARNESS_DIR / "lib.sh"),
            sys.executable,
        ],
        env={"PATH": "/usr/bin:/bin", "DELTA_OUT": str(tmp_path / "delta.json")},
        capture_output=True,
        text=True,
        check=True,
    )
    tmpdir, made = out.stdout.split()
    assert Path(tmpdir).parent == tmp_path and Path(made).parent == Path(tmpdir)


@pytest.fixture
def full_run(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    published = tmp_path / "published"
    published.write_text("accepted generation")
    state = SimpleNamespace(free=2**60, paths=[], cancelled=False)

    def disk_usage(path):
        state.paths.append(Path(path))
        return SimpleNamespace(free=state.free)

    async def apply(*args):
        published.write_text("candidate generation")
        return "applied"

    monkeypatch.setattr(settings, "code_scip_work_dir", str(scratch))
    monkeypatch.setattr(app, "check_held", lambda: None)
    monkeypatch.setattr(app, "move_checkout", AsyncMock())
    monkeypatch.setattr(app, "run_full_harness", AsyncMock(return_value=(0, "complete")))
    monkeypatch.setattr(app, "open_delta", lambda path: object())
    monkeypatch.setattr(app, "apply_delta", AsyncMock(side_effect=apply))
    monkeypatch.setattr(app, "mark_done", AsyncMock())
    monkeypatch.setattr(app.shutil, "disk_usage", disk_usage)
    monkeypatch.setattr(settings, "code_scip_disk_check_interval_s", 0.001)
    repo = SimpleNamespace(set_state=AsyncMock())
    cfg = SimpleNamespace(org="example", namespace="source")

    async def run(**kwargs):
        return await app.full_index(
            repo,
            cfg,
            checkout,
            tmp_path / "mirror",
            tmp_path / "compiler",
            "head",
            timeout_s=10,
            **kwargs,
        )

    return SimpleNamespace(
        run=run,
        state=state,
        checkout=checkout,
        scratch=scratch,
        published=published,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("free", [0, 5 * 1024**3])
async def test_low_space_refuses_before_checkout_or_harness(full_run, free):
    full_run.state.free = free
    with pytest.raises(app.FullIndexFailed, match="disk"):
        await full_run.run()
    app.move_checkout.assert_not_awaited()
    app.run_full_harness.assert_not_awaited()
    app.apply_delta.assert_not_awaited()
    app.mark_done.assert_not_awaited()
    assert full_run.published.read_text() == "accepted generation"
    assert list(full_run.scratch.iterdir()) == []


@pytest.mark.asyncio
async def test_space_drop_cancels_producer_before_temporary_cleanup(full_run, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()
    delta_paths = []

    async def harness(checkout, compiler, delta_path, **kwargs):
        delta_paths.append(delta_path)
        started.set()
        full_run.state.free = 0
        try:
            await asyncio.Event().wait()
        finally:
            assert delta_path.parent.is_dir()
            full_run.state.cancelled = True
            stopped.set()

    monkeypatch.setattr(app, "run_full_harness", harness)
    with pytest.raises(app.FullIndexFailed, match="disk"):
        async with asyncio.timeout(2):
            await full_run.run()
    assert started.is_set() and stopped.is_set() and full_run.state.cancelled
    assert all(not path.parent.exists() for path in delta_paths)
    app.apply_delta.assert_not_awaited()
    app.mark_done.assert_not_awaited()
    assert full_run.published.read_text() == "accepted generation"


@pytest.mark.asyncio
async def test_pressure_at_harness_completion_does_not_publish(full_run, monkeypatch):
    async def harness(*args, **kwargs):
        full_run.state.free = 0
        return 0, "complete"

    monkeypatch.setattr(app, "run_full_harness", harness)
    with pytest.raises(app.FullIndexFailed, match="disk"):
        await full_run.run()
    app.apply_delta.assert_not_awaited()
    app.mark_done.assert_not_awaited()
    assert full_run.published.read_text() == "accepted generation"


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_volume", ["checkout", "scratch"])
async def test_each_writable_volume_is_checked(full_run, monkeypatch, failing_volume):
    def disk_usage(path):
        path = Path(path)
        low = (
            path == full_run.checkout
            if failing_volume == "checkout"
            else path.is_relative_to(full_run.scratch)
        )
        return SimpleNamespace(free=0 if low else 2**60)

    monkeypatch.setattr(app.shutil, "disk_usage", disk_usage)
    with pytest.raises(app.FullIndexFailed, match="disk"):
        await full_run.run()
    app.move_checkout.assert_not_awaited()
    app.apply_delta.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_free_space_fails_closed(full_run, monkeypatch):
    def unavailable(path):
        raise OSError("private volume detail")

    monkeypatch.setattr(app.shutil, "disk_usage", unavailable)
    with pytest.raises(app.FullIndexFailed, match="disk") as exc:
        await full_run.run()
    assert "private volume detail" not in str(exc.value)
    app.move_checkout.assert_not_awaited()
    app.apply_delta.assert_not_awaited()


@pytest.mark.asyncio
async def test_sufficient_space_publishes_and_keeps_existing_success(full_run):
    assert await full_run.run() == ("applied", "complete")
    app.apply_delta.assert_awaited_once()
    app.mark_done.assert_awaited_once()
    assert full_run.published.read_text() == "candidate generation"
    assert list(full_run.scratch.iterdir()) == []


@pytest.mark.asyncio
async def test_checkout_pressure_cancels_before_harness(full_run, monkeypatch):
    stopped = asyncio.Event()

    async def move(*args):
        full_run.state.free = 0
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(app, "move_checkout", move)
    with pytest.raises(app.FullIndexFailed, match="disk"):
        async with asyncio.timeout(2):
            await full_run.run()
    assert stopped.is_set()
    app.run_full_harness.assert_not_awaited()
    app.apply_delta.assert_not_awaited()
    assert list(full_run.scratch.iterdir()) == []


@pytest.mark.asyncio
async def test_pressure_after_checkpoint_copy_does_not_publish(full_run, monkeypatch, tmp_path):
    checkpoint = tmp_path / "checkpoint"

    def keep(path, dest):
        Path(dest).write_text("complete candidate")
        full_run.state.free = 0

    monkeypatch.setattr(app, "_keep", keep)
    with pytest.raises(app.FullIndexFailed, match="disk"):
        await full_run.run(keep_delta=str(checkpoint))
    assert checkpoint.read_text() == "complete candidate"
    assert full_run.published.read_text() == "accepted generation"
    app.apply_delta.assert_not_awaited()
    app.mark_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_cancellation_waits_for_producer_cleanup(full_run, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def harness(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(app, "run_full_harness", harness)
    job = asyncio.create_task(full_run.run())
    try:
        async with asyncio.timeout(2):
            await started.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert stopped.is_set()
        assert list(full_run.scratch.iterdir()) == []
        app.apply_delta.assert_not_awaited()
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_disk_pressure_uses_original_job_child_cleanup(full_run, monkeypatch, tmp_path):
    from server.code.app.index import sandbox

    pidfile = tmp_path / "child.pid"
    observed = {}

    def disk_usage(path):
        if pidfile.exists():
            text = pidfile.read_text()
            if text.isdigit():
                if "pidfd" not in observed:
                    observed["pidfd"] = os.pidfd_open(int(text))
                return SimpleNamespace(free=0)
        return SimpleNamespace(free=2**60)

    async def harness(*args, **kwargs):
        result = await sandbox.run_job(
            [
                sys.executable,
                "-c",
                "import os,pathlib,time; pathlib.Path("
                + repr(str(pidfile))
                + ").write_text(str(os.getpid())); time.sleep(30)",
            ],
            env={"PATH": os.defpath},
            timeout_s=10,
        )
        return result.returncode, result.stdout.decode()

    monkeypatch.setattr(sandbox, "sandbox_available", lambda: False)
    monkeypatch.setattr(settings, "code_sandbox_required", False)
    monkeypatch.setattr(app.shutil, "disk_usage", disk_usage)
    monkeypatch.setattr(app, "run_full_harness", harness)
    try:
        with pytest.raises(app.FullIndexFailed, match="disk"):
            async with asyncio.timeout(3):
                await full_run.run()
        poll = select.poll()
        poll.register(observed["pidfd"], select.POLLIN)
        assert poll.poll(0), "the original job teardown must reap its child"
        assert list(full_run.scratch.iterdir()) == []
        assert full_run.published.read_text() == "accepted generation"
        app.apply_delta.assert_not_awaited()
    finally:
        if "pidfd" in observed:
            os.close(observed["pidfd"])


@pytest.mark.parametrize("reserve", [0, -1, float("inf"), float("nan")])
def test_disk_reserve_is_positive_and_finite(reserve):
    from server.settings import Settings

    with pytest.raises(ValueError, match="code_scip_min_free_disk_gb"):
        Settings(code_scip_min_free_disk_gb=reserve)


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), 0.5001])
def test_disk_sampling_period_is_positive_finite_and_bounded(interval):
    from server.settings import Settings

    with pytest.raises(ValueError, match="code_scip_disk_check_interval_s"):
        Settings(code_scip_disk_check_interval_s=interval)
