"""The C++ full re-index of one entry: the checkout moved to a commit, the whole
tree through scip-clang, the delta applied and `scip_commit` set. The caller holds
the entry lock; `loom code scip-full` and the code_scip_full worker run it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from server.code.app.index import compiler_tier
from server.code.app.index.delta_file import open_delta
from server.code.app.index.entry_lock import check_held
from server.code.app.index.gitrepo import git_config_env
from server.code.app.index.sandbox import tool_mounts
from server.code.app.index.scip_delta import ScipApplyStats, apply_delta
from server.code.app.index.tier_health import ToolOutputError
from server.code.app.index.untrusted_files import FileRefused, open_regular
from server.core.paths import SCIP_HARNESS_DIR
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_index.scip_full")

HARNESS = SCIP_HARNESS_DIR / "run_delta.sh"
WATERMARK = "scip_commit"
# Unix seconds of the last full re-index attempt (its start, or its end when it
# failed), and of the last one applied.
FULL_AT = "scip_full_at"
FULL_DONE_AT = "scip_full_done_at"
PRODUCER = "clang"


# A run's directory left this long is a crashed run's: past the longest a full run
# may take (its pass deadline), never less than a day.
def _stale_s() -> float:
    return max(24 * 3600.0, settings.code_scip_full_timeout_seconds + 6 * 3600.0)


def work_dir() -> str | None:
    """`code_scip_work_dir`, made if missing, without crashed runs' leftovers
    (it is on the cache volume, which outlives a container restart); None = the
    system temp dir."""
    if not settings.code_scip_work_dir:
        return None
    path = Path(settings.code_scip_work_dir).absolute()
    path.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - _stale_s()
    for old in path.glob("scip_*"):
        with contextlib.suppress(OSError):
            if old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
    return str(path)


class FullIndexFailed(ToolOutputError):
    def __init__(self, returncode: int, tail: str) -> None:
        super().__init__(f"harness exit {returncode}: {tail[-2000:]}")
        self.returncode = returncode
        self.tail = tail


async def move_checkout(checkout: Path, bare: Path, target: str, cfg: RepoConfig) -> None:
    """Fetch `target` from the bare clone, force-check it out, then the submodules
    exactly at their pins. Never recurse submodules on the fetch: one pin rewritten
    away upstream fatals the whole fetch. Each step gets a real budget and one retry
    (the delta's 600 s is sized for a few merges); the fetch reads the bare clone,
    the submodule pins come from the network."""
    await compiler_tier.git(
        checkout,
        *compiler_tier.checkout_fetch_args(bare, target),
        local_remote=bare,
        timeout_s=settings.code_git_fetch_timeout_s,
        retries=1,
    )
    await compiler_tier.git(
        checkout,
        "checkout",
        "--force",
        target,
        timeout_s=settings.code_git_fetch_timeout_s,
        retries=1,
    )
    await compiler_tier.git(
        checkout,
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--quiet",
        network=True,
        token=cfg.github_token,
        remote=cfg.git_remote,
        timeout_s=settings.code_git_fetch_timeout_s,
        retries=1,
    )


async def run_full_harness(
    checkout: Path, scip_clang: Path, delta_path: Path, *, timeout_s: float, jobs: int = 0
) -> tuple[int, str]:
    """The full-index harness as a repository job, writing `delta_path`:
    (exit code, end of its output), 124 on a timeout."""
    env = {
        **git_config_env(),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHON": sys.executable,
        "DELTA_OUT": str(delta_path),
        "SCIP_CLANG": str(scip_clang),
        "HOME": str(Path.home()),
    }
    if jobs:
        env["JOBS"] = str(jobs)
    try:
        return await compiler_tier.run_harness(
            HARNESS,
            checkout,
            ["--full", "", "-"],
            env,
            out_dir=delta_path.parent,
            timeout_s=timeout_s,
            readonly=tool_mounts(scip_clang),
        )
    except RuntimeError as e:  # run_harness names its timeout this way
        return 124, str(e)


async def full_index(
    repo: CodeRepo,
    cfg: RepoConfig,
    checkout: Path,
    bare: Path,
    scip_clang: Path,
    target: str,
    *,
    timeout_s: float,
    jobs: int = 0,
    keep_delta: str = "",
) -> tuple[ScipApplyStats, str]:
    """Re-index the entry at `target` in full: the apply's stats and the end of the
    harness output; `FullIndexFailed` on a harness failure. `keep_delta` copies the
    delta there before the apply."""
    check_held()
    await repo.set_state(cfg.org, cfg.namespace, FULL_AT, str(int(time.time())))
    try:
        return await _index_and_apply(
            repo, cfg, checkout, bare, scip_clang, target, timeout_s, jobs, keep_delta
        )
    except Exception:
        # the retry window runs from the failure, not from a start hours earlier
        try:
            await repo.set_state(cfg.org, cfg.namespace, FULL_AT, str(int(time.time())))
        except Exception as e:  # noqa: BLE001 — the run's own error is the one to raise
            logger.warning("scip-full %s: failure time not recorded: %s", cfg.namespace, e)
        raise


async def _index_and_apply(
    repo: CodeRepo,
    cfg: RepoConfig,
    checkout: Path,
    bare: Path,
    scip_clang: Path,
    target: str,
    timeout_s: float,
    jobs: int,
    keep_delta: str,
) -> tuple[ScipApplyStats, str]:
    await move_checkout(checkout, bare, target, cfg)
    with tempfile.TemporaryDirectory(prefix="scip_full_", dir=work_dir()) as tmp:
        delta_path = Path(tmp) / "delta.json"
        rc, out = await run_full_harness(
            checkout, scip_clang, delta_path, timeout_s=timeout_s, jobs=jobs
        )
        if rc != 0:
            raise FullIndexFailed(rc, out)
        if keep_delta:  # kept as the harness wrote it, before it is parsed
            await asyncio.to_thread(_keep, delta_path, keep_delta)
        delta = await asyncio.to_thread(open_delta, delta_path)
        # applied while the file is there: the rounds are read from it
        stats = await apply_delta(repo, cfg.org, cfg.namespace, delta)
    check_held()
    await mark_done(repo, cfg.org, cfg.namespace, target)
    return stats, out


def _keep(delta_path: Path, keep_delta: str) -> None:
    """Copy the harness's delta, refused when it is a link, not a regular file or over
    `code_scip_delta_file_max_mb`. The copy is written beside `keep_delta` and moved
    onto it only once whole, so a failed copy leaves whatever was at that path. Only the
    user that ran the command can read it (mode 0600): a delta holds the indexed
    repository's paths and symbol names."""
    fd, st = open_regular(delta_path, settings.code_scip_delta_file_max_mb * 1024 * 1024)
    dest = Path(keep_delta)
    with open(fd, "rb") as src:
        tmp_fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
        try:
            with open(tmp_fd, "wb") as dst:
                left = st.st_size  # never past the size it was opened at
                while left and (data := src.read(min(left, 1024 * 1024))):
                    dst.write(data)
                    left -= len(data)
                if left or src.read(1):
                    raise FileRefused(f"{delta_path} changed while it was read")
            os.replace(tmp_name, dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


async def mark_done(repo: CodeRepo, org: str, ns: str, commit: str) -> None:
    """A full index applied at `commit`: the watermark and the weekly clock."""
    await repo.set_state(org, ns, WATERMARK, commit)
    await repo.set_state(org, ns, FULL_DONE_AT, str(int(time.time())))
