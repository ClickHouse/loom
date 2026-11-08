"""Per-merge compiler-tier freshness for TypeScript.

`code_scip_delta` keeps the C++ compiler tier current by running scip-clang
over the changed translation units when the fast tier's `git_head` outruns
`scip_commit`. This worker is the TypeScript row: for every repository
entry whose `scip_producer` is "typescript" it compares `git_head` with
`scip_commit_ts`, moves the configured checkout to `git_head` (fetching
from the LOCAL bare clone code_ingest already advanced — never the
network), runs `run_typescript_delta.sh` in DELTA_OUT mode (scip-typescript
over the workspace, the converter's `--changed-files` bounding the delta to
the window's TypeScript files; the first run with no watermark is a full
index) and applies the JSON delta in-process through the same validated
apply path the upload ops use.

scip-typescript has no per-file mode, so every pass re-indexes the
workspace (~4–5 min for a 5k-file workspace); one pass covers every merge
since the watermark, so burst-hours coalesce into one run. The worker prepares
checkout dependencies before indexing and refreshes them when their inputs change.
Package lifecycle scripts are disabled; the image ships the package managers.

House rule: the SERVER never parses protobuf — scip-typescript
and the conversion run in subprocesses; the worker consumes the JSON delta.
The worker is enabled by default and skips entries whose checkout or
scip-typescript binary are absent. Dependency failures leave the watermark intact.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from server.code.app.index.compiler_tier import (
    checkout_fetch_args,
    compiler_target,
    git,
    gone_documents,
    harness_failure,
    resolve_binary,
    resolve_checkout,
    run_harness,
    run_tier,
)
from server.code.app.index.gitrepo import auth_env, communicate_or_kill, git_config_env
from server.code.app.index.sandbox import tool_mounts
from server.code.app.index.typescript_dependencies import (
    LOCKFILES,
    prepare_dependencies,
)
from server.code.app.index.untrusted_files import read_delta
from server.core.paths import SCIP_HARNESS_DIR
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.workers.code_scip_ts_delta")

_HARNESS = SCIP_HARNESS_DIR / "run_typescript_delta.sh"
PRODUCER = "typescript"
WATERMARK = "scip_commit_ts"


_LOCKFILES = LOCKFILES


@dataclass
class CodeScipTsDeltaResult:
    repos_checked: int = 0
    deltas_applied: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Windows in which a lockfile changed and dependency preparation was refreshed.
    lockfile_changed: list[str] = field(default_factory=list)


async def run_pass() -> CodeScipTsDeltaResult:
    return await run_tier(
        "code_scip_ts_delta",
        PRODUCER,
        WATERMARK,
        CodeScipTsDeltaResult(),
        _run_repo,
        gc_checkout=True,
        extras=lambda r: {"lockfile_changed": r.lockfile_changed[:5]},
    )


def _resolve_paths(cfg: RepoConfig) -> tuple[Path, Path, str | None]:
    """(checkout, bare clone, missing); dependencies are prepared after checkout."""
    checkout, bare, missing = resolve_checkout(cfg, PRODUCER)
    if missing:
        return checkout, bare, missing
    binary = resolve_binary(settings.code_scip_typescript_path)
    if not binary.exists():
        return checkout, bare, f"scip-typescript missing at {binary}"
    return checkout, bare, None


async def _run_repo(repo: CodeRepo, cfg: RepoConfig, result: CodeScipTsDeltaResult) -> None:
    target = await compiler_target(repo, cfg)
    if not target:
        result.skipped.append(f"{cfg.namespace}: git_head not seeded")
        return
    scip_commit = await repo.get_state(cfg.org, cfg.namespace, WATERMARK) or ""
    if target == scip_commit:
        return  # compiler tier is current
    checkout, bare, missing = await asyncio.to_thread(_resolve_paths, cfg)
    if missing:
        result.skipped.append(f"{cfg.namespace}: {missing}")
        return
    await git(checkout, *checkout_fetch_args(bare, target), local_remote=bare)
    await git(checkout, "checkout", "--force", target)
    lockfile_changed = bool(scip_commit and await _lockfile_changed(checkout, scip_commit, target))
    if lockfile_changed:
        note = (
            f"{cfg.namespace}: a lockfile changed in {scip_commit[:8]}..{target[:8]}"
            " — refreshing checkout dependencies automatically"
        )
        result.lockfile_changed.append(note)
        logger.warning("code_scip_ts_delta: %s", note)

    env = await asyncio.to_thread(_harness_env, Path("unused"))
    await prepare_dependencies(checkout, cfg.scip_ts_projects, env, force=lockfile_changed)

    with tempfile.TemporaryDirectory(prefix="scip_ts_delta_") as tmp:
        delta_path = Path(tmp) / "delta.json"
        await _run_harness(
            checkout,
            scip_commit or "--full",
            target,
            delta_path,
            cfg.scip_ts_projects,
        )
        try:
            raw = await asyncio.to_thread(read_delta, delta_path)
        except FileNotFoundError:
            # the harness exits 0 with no delta when no TypeScript file changed
            logger.info("code_scip_ts_delta: %s no TypeScript changes in window", cfg.namespace)
            await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
            return
        from server.code.app.index.scip_delta import apply_delta, parse_delta

        delta = parse_delta(raw)
        del raw  # the bytes are not needed past the parse

    if not scip_commit:
        # A full run sees the whole tree, so a document with
        # live compiler rows that the new index does not emit is gone
        # (moved or deleted since the last full run); retire it
        delta.deleted_files = gone_documents(
            await repo.live_scip_paths(cfg.org, cfg.namespace),
            delta,
            PRODUCER,
        )
    stats = await apply_delta(repo, cfg.org, cfg.namespace, delta)
    await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
    result.deltas_applied += 1
    logger.info(
        "code_scip_ts_delta: %s %s..%s applied %s",
        cfg.namespace,
        (scip_commit or "full")[:8],
        target[:8],
        stats.model_dump(),
    )


def _harness_env(delta_path: Path, projects: tuple[str, ...] = ()) -> dict[str, str]:
    """The harness's environment (sync: resolves the binary's path)."""
    # `.absolute()`, never `.resolve()`: an npm-installed scip-typescript is a
    # symlink under node_modules/.bin, and the pnpm shim the harness needs
    # sits beside the LINK, not beside its target
    binary = resolve_binary(settings.code_scip_typescript_path)
    env = {
        **git_config_env(),
        # the binary's directory first: an npm-installed scip-typescript sits
        # beside the pnpm / node shims the harness needs
        "PATH": f"{binary.parent}:{os.environ.get('PATH', os.defpath)}",
        "PYTHON": sys.executable,  # converter needs the server's venv (protobuf runtime)
        "DELTA_OUT": str(delta_path),
        "SCIP_TYPESCRIPT": str(binary),
        "SCIP_TS_PATHS": "1" if settings.code_scip_ts_paths else "0",
        "NODE_HEAP_MB": str(settings.code_scip_ts_node_heap_mb),
        "HOME": str(Path.home()),
    }
    if projects:
        env["SCIP_TS_PROJECTS"] = " ".join(projects)
    if "NODE_OPTIONS" in os.environ:
        env["NODE_OPTIONS"] = os.environ["NODE_OPTIONS"]
    return env


async def _lockfile_changed(checkout: Path, base: str, head: str) -> bool:
    """Did any package-manager lockfile change between the two commits?"""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(checkout),
        "diff",
        "--name-only",
        "--no-renames",
        base,
        head,
        "--",
        *(f":(glob)**/{lock}" for lock in _LOCKFILES),  # a project's own lockfile too
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=auth_env(),
    )
    out, _err = await communicate_or_kill(proc, timeout_s=settings.code_git_timeout_s)
    return bool(out.strip())


async def _run_harness(
    checkout: Path, base: str, head: str, delta_path: Path, projects: tuple[str, ...] = ()
) -> None:
    """`run_typescript_delta.sh` in DELTA_OUT mode: index, convert, write
    the JSON delta — no upload, no watermark read (the worker owns both)."""
    env = await asyncio.to_thread(_harness_env, delta_path, projects)
    binary = await asyncio.to_thread(resolve_binary, settings.code_scip_typescript_path)
    returncode, out = await run_harness(
        _HARNESS,
        checkout,
        [base, head],
        env,
        out_dir=delta_path.parent,
        timeout_s=settings.code_scip_ts_delta_timeout_seconds,
        readonly=tool_mounts(binary.parent),
    )
    if returncode != 0:
        raise harness_failure(returncode, out[-2000:])


__all__ = ["PRODUCER", "WATERMARK", "CodeScipTsDeltaResult", "run_pass"]
