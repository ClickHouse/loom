"""C++ full re-index, one entry per pass.

An entry is due when its C++ tier has no `scip_commit` yet (the delta worker
waits for one) or its last applied full run is `code_scip_full_every_hours` old:
the full run heals deltas that hit the TU cap and any drift. A run that fails is
retried 6 hours after it failed; one killed with its pod, 6 hours after it
started. It runs under the entry lock, so the delta skips that entry meanwhile
and every other entry stays fresh.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from server.code.app.index.compiler_tier import (
    compiler_target,
    note_stale_compdb,
    resolve_binary,
    resolve_checkout,
    run_tier,
)
from server.code.app.index.scip_full import FULL_AT, FULL_DONE_AT, WATERMARK, full_index
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.workers.code_scip_full")

PRODUCER = "clang"


@dataclass
class CodeScipFullResult:
    repos_checked: int = 0
    deltas_applied: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # entries a full run started on: one per pass, whether it succeeds or not
    started: int = 0


async def run_pass() -> CodeScipFullResult:
    return await run_tier(
        "code_scip_full",
        PRODUCER,
        WATERMARK,
        CodeScipFullResult(),
        _run_repo,
        entry_locked=True,
        runs_repo_code=True,
        gc_checkout=True,
    )


async def _due(repo: CodeRepo, cfg: RepoConfig) -> bool:
    """No attempt within `code_scip_full_retry_s` (`FULL_AT` is a run's start, moved to its end
    when it fails; a run killed with its pod counts from its start), and
    unseeded or last applied `every_hours` ago (0 = seed only)."""
    now = time.time()
    started = await repo.get_state(cfg.org, cfg.namespace, FULL_AT)
    if now - float(started or 0) < settings.code_scip_full_retry_s:
        return False
    if not await repo.get_state(cfg.org, cfg.namespace, WATERMARK):
        return True
    every = settings.code_scip_full_every_hours * 3600
    done = await repo.get_state(cfg.org, cfg.namespace, FULL_DONE_AT)
    return every > 0 and now - float(done or 0) >= every


def _resolve_paths(cfg: RepoConfig) -> tuple[Path, Path, Path, str | None]:
    checkout, bare, missing = resolve_checkout(cfg, PRODUCER)
    scip_clang = resolve_binary(settings.code_scip_clang_path)
    if not missing and not scip_clang.exists():
        missing = f"scip-clang missing at {scip_clang}"
    return checkout, bare, scip_clang, missing


async def _run_repo(repo: CodeRepo, cfg: RepoConfig, result: CodeScipFullResult) -> None:
    if result.started:
        return  # one entry per pass
    target = await compiler_target(repo, cfg)
    if not target:
        result.skipped.append(f"{cfg.namespace}: git_head not seeded")
        return
    if not await _due(repo, cfg):
        return
    checkout, bare, scip_clang, missing = await asyncio.to_thread(_resolve_paths, cfg)
    if missing:
        result.skipped.append(f"{cfg.namespace}: {missing}")
        return
    result.started += 1
    stats, out = await full_index(
        repo,
        cfg,
        checkout,
        bare,
        scip_clang,
        target,
        timeout_s=settings.code_scip_full_timeout_seconds,
        jobs=settings.code_scip_clang_jobs,
    )
    note_stale_compdb("code_scip_full", cfg, out, result.skipped)
    result.deltas_applied += 1
    logger.info(
        "code_scip_full: %s at %s applied %s", cfg.namespace, target[:8], stats.model_dump()
    )
