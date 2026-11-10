"""Python compiler-tier deltas per merge.

The parser tier binds a Python call through its imports, receivers, chains
and heritage and still misses the calls only
inference types — attributes set on a base in another file, unannotated
parameters, metaclass-built APIs — the compiler tier's ground. Without this
worker an operator runs `scripts/scip_harness/run_python_delta.sh` against
the server with a writer token by hand. For a repository entry whose
`scip_producer` is "python" (explicit, or inferred from its manifests) this
worker compares `git_head` with `scip_commit_py` and,
when the head has moved, runs scip-python and converts the index to a delta
bounded to the Python files the window touched (a first run indexes the
whole tree) in DELTA_OUT mode, then applies it in process — the TypeScript
worker's shape without an install step: scip-python reads
the repository's own modules from the tree and third-party packages from an
environment file the operator may leave empty.
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
from server.code.app.index.delta_file import open_delta
from server.code.app.index.gitrepo import git_config_env
from server.code.app.index.sandbox import tool_mounts
from server.core.paths import SCIP_HARNESS_DIR
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.workers.code_scip_py_delta")

_HARNESS = SCIP_HARNESS_DIR / "run_python_delta.sh"
PRODUCER = "python"
WATERMARK = "scip_commit_py"


@dataclass
class CodeScipPyDeltaResult:
    repos_checked: int = 0
    deltas_applied: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def run_pass() -> CodeScipPyDeltaResult:
    return await run_tier(
        "code_scip_py_delta",
        PRODUCER,
        WATERMARK,
        CodeScipPyDeltaResult(),
        _run_repo,
        gc_checkout=True,
    )


def _resolve_paths(cfg: RepoConfig) -> tuple[Path, Path, str | None]:
    """(checkout, bare clone, missing), including the scip-python binary."""
    checkout, bare, missing = resolve_checkout(cfg, PRODUCER)
    if missing:
        return checkout, bare, missing
    binary = resolve_binary(settings.code_scip_python_path)
    if not binary.exists():
        return checkout, bare, f"scip-python missing at {binary}"
    return checkout, bare, None


async def _run_repo(repo: CodeRepo, cfg: RepoConfig, result: CodeScipPyDeltaResult) -> None:
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
    with tempfile.TemporaryDirectory(prefix="scip_py_delta_") as tmp:
        delta_path = Path(tmp) / "delta.json"
        await _run_harness(checkout, scip_commit or "--full", target, delta_path)
        try:
            delta = await asyncio.to_thread(open_delta, delta_path)
        except FileNotFoundError:
            # the harness exits 0 with no delta when no Python file changed
            logger.info("code_scip_py_delta: %s no Python changes in window", cfg.namespace)
            await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
            return
        from server.code.app.index.scip_delta import apply_delta

        if not scip_commit:
            # A full run sees the whole tree; a document with live
            # compiler rows the new index does not emit is gone — retire it
            delta.deleted_files = gone_documents(
                await repo.live_scip_paths(cfg.org, cfg.namespace),
                delta,
                PRODUCER,
            )
        stats = await apply_delta(repo, cfg.org, cfg.namespace, delta)
    await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
    result.deltas_applied += 1
    logger.info(
        "code_scip_py_delta: %s %s..%s applied %s",
        cfg.namespace,
        (scip_commit or "full")[:8],
        target[:8],
        stats.model_dump(),
    )


def _harness_env(delta_path: Path) -> dict[str, str]:
    """The harness's environment (sync: resolves the binary's path). The
    binary's directory leads PATH so an npm-installed scip-python finds the
    node it was installed beside."""
    binary = resolve_binary(settings.code_scip_python_path)
    env = {
        **git_config_env(),
        "PATH": f"{binary.parent}:/usr/local/bin:/usr/bin:/bin",
        "PYTHON": sys.executable,  # converter needs the server's venv (protobuf runtime)
        "DELTA_OUT": str(delta_path),
        "SCIP_PYTHON": str(binary),
        "SCIP_PYTHON_HEAP_MB": str(settings.code_scip_py_heap_mb),
        "HOME": str(Path.home()),
    }
    for key in ("NODE_OPTIONS", "SCIP_ENV_JSON"):  # the operator's node flags / environment file
        if key in os.environ:
            env[key] = os.environ[key]
    return env


async def _run_harness(checkout: Path, base: str, head: str, delta_path: Path) -> None:
    """`run_python_delta.sh` in DELTA_OUT mode: index, convert, write the
    JSON delta — no upload, no watermark read (the worker owns both)."""
    env = await asyncio.to_thread(_harness_env, delta_path)
    binary = await asyncio.to_thread(resolve_binary, settings.code_scip_python_path)
    readonly = tool_mounts(binary.parent)
    if "SCIP_ENV_JSON" in env:
        readonly.append(Path(env["SCIP_ENV_JSON"]))
    returncode, out = await run_harness(
        _HARNESS,
        checkout,
        [base, head],
        env,
        out_dir=delta_path.parent,
        timeout_s=settings.code_scip_py_delta_timeout_seconds,
        readonly=readonly,
    )
    if returncode != 0:
        raise harness_failure(returncode, out[-2000:])


__all__ = ["PRODUCER", "WATERMARK", "CodeScipPyDeltaResult", "run_pass"]
