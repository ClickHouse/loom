"""Per-merge compiler-tier freshness: scip-clang deltas.

The fast (tree-sitter) tier is per-merge fresh via code_ingest; this worker
closes the gap for the compiler-exact tier. Each pass compares the branch
head in the entry's mirror (`compiler_target`) with the compiler tier's
`scip_commit`; when they diverge it updates the configured checkout, runs the scip harness over
the changed translation units (run_delta.sh: changed .cpp plus TUs
including changed headers, capped — above the cap it degrades to changed
.cpp only, healed by the next --full run), reads the resulting JSON delta,
and applies it in-process through the same validated apply path the upload
ops use (declaration filtering, path-prefix scope enforcement).

House rule: the SERVER never parses protobuf — scip-clang and the
protobuf→JSON conversion run in subprocesses; the worker consumes only the
JSON delta, the format the apply path already speaks.

Batching is inherent: one pass covers every merge since `scip_commit`, so
burst-hours coalesce into one scip-clang run. The worker is enabled by default;
an entry skips cleanly while the binary is missing or before code_scip_full has
seeded `scip_commit`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from server.code.app.index.compiler_tier import (
    checkout_fetch_args,
    compiler_target,
    git,
    harness_failure,
    raise_if_stale_compdb,
    resolve_binary,
    resolve_checkout,
    run_harness,
    run_tier,
    stale_compdb_pending,
)
from server.code.app.index.entry_lock import check_held
from server.code.app.index.gitrepo import git_config_env
from server.code.app.index.sandbox import tool_mounts
from server.code.app.index.scip_full import work_dir
from server.core.paths import SCIP_HARNESS_DIR
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.workers.code_scip_delta")

_HARNESS = SCIP_HARNESS_DIR / "run_delta.sh"
PRODUCER = "clang"
WATERMARK = "scip_commit"


@dataclass
class CodeScipDeltaResult:
    repos_checked: int = 0
    deltas_applied: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def run_pass() -> CodeScipDeltaResult:
    # The entry lock makes an entry whose checkout a full re-index is moving a skip
    # until the next pass.
    return await run_tier(
        "code_scip_delta",
        PRODUCER,
        WATERMARK,
        CodeScipDeltaResult(),
        _run_repo,
        entry_locked=True,
        runs_repo_code=True,
    )


def _resolve_paths(cfg: RepoConfig) -> tuple[Path, str, Path, str | None]:
    """(checkout, scip-clang, bare clone, missing), in a thread like every tier."""
    checkout, bare, missing = resolve_checkout(cfg, PRODUCER)
    if missing:
        return checkout, "", bare, missing
    scip_clang = resolve_binary(settings.code_scip_clang_path)
    if not scip_clang.exists():
        return checkout, "", bare, f"scip-clang missing at {scip_clang}"
    return checkout, str(scip_clang.resolve()), bare, None


async def _run_repo(repo: CodeRepo, cfg: RepoConfig, result: CodeScipDeltaResult) -> bool | None:
    target = await compiler_target(repo, cfg)
    scip_commit = await repo.get_state(cfg.org, cfg.namespace, WATERMARK)
    if not target or not scip_commit:
        # code_scip_full seeds scip_commit with the entry's first full index
        result.skipped.append(f"{cfg.namespace}: watermarks not seeded")
        return
    if target == scip_commit:
        # current: a record failing on a stale compile database stays until a configure
        # succeeds (False leaves it)
        return not await asyncio.to_thread(stale_compdb_pending, cfg)

    checkout, scip_clang_abs, bare, missing = await asyncio.to_thread(_resolve_paths, cfg)
    if missing:
        result.skipped.append(f"{cfg.namespace}: {missing}")
        return

    # Move the checkout to the target. Fetch from the LOCAL bare
    # clone (already advanced by code_ingest) — never the network. Submodule
    # recursion is explicitly OFF: git's default on-demand recursion tries to
    # fetch every pin that changed anywhere in the fetched range, so a single
    # pin the upstream submodule has since rewritten away fatals the whole
    # fetch and stalls the compiler tier. Only `submodule update` below needs
    # pins, and only the ones at the target itself; it fetches the URLs the
    # repository's .gitmodules names, so it goes through the egress proxy.
    await git(checkout, *checkout_fetch_args(bare, target), local_remote=bare)
    await git(checkout, "checkout", "--force", target)
    await git(
        checkout,
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--quiet",
        network=True,
        token=cfg.github_token,
        remote=cfg.git_remote,
    )

    with tempfile.TemporaryDirectory(prefix="scip_delta_", dir=work_dir()) as tmp:
        delta_path = Path(tmp) / "delta.json"
        returncode, out = await run_harness(
            _HARNESS,
            checkout,
            [scip_commit, target, "-"],
            {
                **git_config_env(),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                # converter needs the server's venv (protobuf runtime)
                "PYTHON": sys.executable,
                "DELTA_OUT": str(delta_path),
                "SCIP_CLANG": scip_clang_abs,
                "MAX_TUS": str(settings.code_scip_delta_max_tus),
                **({"JOBS": str(jobs)} if (jobs := settings.code_scip_clang_jobs) else {}),
                "HOME": str(Path.home()),
            },
            out_dir=Path(tmp),
            timeout_s=settings.code_scip_delta_timeout_seconds,
            readonly=tool_mounts(Path(scip_clang_abs)),
        )
        if returncode != 0:
            # base unknown clears scip_commit: code_scip_full re-indexes the entry
            raise harness_failure(returncode, out[-2000:])

        if not await asyncio.to_thread(delta_path.exists):
            # harness legitimately exits 0 with no delta when no C++ TU changed
            logger.info("code_scip_delta: %s no C++ changes in window", cfg.namespace)
            check_held()
            await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
            raise_if_stale_compdb(out)
            return None

        from server.code.app.index.scip_delta import ScipDelta, apply_delta

        raw = await asyncio.to_thread(delta_path.read_text)
        delta = ScipDelta.model_validate(json.loads(raw))

    stats = await apply_delta(repo, cfg.org, cfg.namespace, delta)
    check_held()
    await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
    result.deltas_applied += 1
    logger.info(
        "code_scip_delta: %s %s..%s applied %s",
        cfg.namespace,
        scip_commit[:8],
        target[:8],
        stats.model_dump(),
    )
    raise_if_stale_compdb(out)
    return None
