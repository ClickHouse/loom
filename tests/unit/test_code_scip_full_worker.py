"""code_scip_full seeds and re-indexes one due entry per pass; code_crossref
runs once per git_head."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index.scip_delta import ScipApplyStats
from server.code.workers import crossref, scip_full
from tests.unit.code_fixtures import repo_config


@pytest.fixture(autouse=True)
def _lock_dir(monkeypatch, tmp_path):
    from server.settings import settings
    from tests.unit.claims_fake import held_leases

    monkeypatch.setattr(settings, "code_scip_lock_dir", str(tmp_path / "locks"))
    held_leases(monkeypatch)


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
async def test_a_full_run_on_a_stale_compile_database_fails_the_full_record(tmp_path, monkeypatch):
    """The harness leaves its marker in the checkout when CMake cannot reconfigure."""
    from server.code.app.index.compiler_tier import STALE_COMPDB_MARKER

    cfg = _cfg(tmp_path, "code-a")
    marker = tmp_path / "checkout-code-a" / STALE_COMPDB_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("CMake Error: a package is missing\n")
    record = AsyncMock()
    with patch("server.code.app.index.tier_health.record", record):
        res, _ = await _full_pass(tmp_path, monkeypatch, {("code-a", "git_head"): "a" * 40}, [cfg])
    assert res.deltas_applied == 1 and not res.errors
    (_, _, ns, tier, err) = record.await_args.args
    assert (ns, tier) == ("code-a", "clang_full") and "a package is missing" in str(err)


@pytest.mark.asyncio
async def test_a_manual_full_run_on_a_stale_compile_database_says_so(tmp_path, monkeypatch, capsys):
    """`loom code scip-full` prints the stale database, as the workers' skip does."""
    import argparse

    from server.cli import code_scip
    from server.code.app.index import scip_full as app
    from server.code.app.index.compiler_tier import STALE_COMPDB_MARKER
    from server.settings import settings

    cfg = _cfg(tmp_path, "code-a")
    checkout = tmp_path / "checkout-code-a"
    (checkout / STALE_COMPDB_MARKER).parent.mkdir(parents=True)
    (checkout / STALE_COMPDB_MARKER).touch()
    clang = tmp_path / "scip-clang"
    clang.touch()
    monkeypatch.setattr(settings, "code_scip_clang_path", str(clang))
    monkeypatch.setattr(app, "full_index", AsyncMock(return_value=(ScipApplyStats(), "")))
    args = argparse.Namespace(checkout=str(checkout), apply_delta="", jobs=0, keep_delta="")
    record = AsyncMock()
    with (
        patch(
            "server.code.store.index.CodeRepo",
            return_value=_repo({("code-a", "git_head"): "a" * 40}),
        ),
        patch("server.code.app.index.tier_health.record", record),
    ):
        rc = await code_scip._scip_full_locked(args, "o", "code-a", cfg)
    assert rc == 0
    assert "code-a: cmake reconfigure failed; compile database stale" in capsys.readouterr().err
    (_, _, _, tier, err) = record.await_args.args
    assert tier == "clang_full" and "cmake reconfigure failed" in err


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
