"""`loom code scip-*` and `entries`: the compiler-tier full and delta
passes the host crons run, and failed-batch retry."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from server.cli.code_backfill import _record_manual_pass
from server.cli.code_run import _identity, _note, _repo_cfg
from server.core.paths import REPO_ROOT

# The harness's own bound; deploy/cron/scip-full.sh's SCIP_FULL_ENTRY_TIMEOUT
# (8h) bounds the whole command too.
_FULL_HARNESS_TIMEOUT_S = 8 * 3600


async def _scip_move_checkout(checkout: Any, bare: Any, git_head: str) -> None:
    """Move the scip working tree to `git_head`: fetch from the bare clone,
    force-checkout, then the submodules exactly at their pins."""
    from server.code.app.index.compiler_tier import checkout_fetch_args, git

    print(f"moving checkout to {git_head[:12]}…")
    # Same rule as the delta worker: never recurse submodules on the
    # fetch — one rewritten-away contrib pin fatals the whole fetch, and the
    # `submodule update` below fetches exactly the pins git_head needs.
    # A full pass is an hour-long job; give the three checkout steps a real
    # budget and one retry (the worker's 600 s is sized for a 30-min cadence).
    await git(checkout, *checkout_fetch_args(bare, git_head), timeout_s=1800, retries=1)
    await git(checkout, "checkout", "--force", git_head, timeout_s=1800, retries=1)
    await git(
        checkout,
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--quiet",
        timeout_s=3600,
        retries=1,
    )


async def _scip_apply_only(
    args: argparse.Namespace,
    repo: Any,
    org: str,
    ns: str,
    git_head: str,
    cfg: Any = None,
) -> int:
    """`scip-full --apply-delta`: apply a delta the harness already produced
    (`--keep-delta`). The apply is idempotent — a re-run key-diffs against
    the live rows and writes only what is missing — so an interrupted first
    apply resumes here without paying the index step again."""
    import json as _json
    from pathlib import Path

    from server.code.app.index.scip_delta import ScipDelta, apply_delta
    from server.code.workers.scip_apply import watermark_key_for

    raw = await asyncio.to_thread(Path(args.apply_delta).read_text)
    delta = ScipDelta.model_validate(_json.loads(raw))
    if delta.commit != git_head:
        print(
            f"note: delta is at {delta.commit[:12]}, git_head is {git_head[:12]} — "
            "applying the delta's commit",
            file=sys.stderr,
        )
    print(f"applying {args.apply_delta} ({len(delta.files)} files)…")
    stats = await apply_delta(repo, org, ns, delta)
    # The watermark of the entry's own tier advances (a TypeScript delta moves
    # the TypeScript watermark, never the C++ one).
    key = watermark_key_for(cfg.scip_producer or "clang")
    await repo.set_state(org, ns, key, delta.commit)
    print(f"applied: {stats.model_dump()}")
    print(f"{key} -> {delta.commit[:12]}")
    _note(head=delta.commit[:12], harness_exit=0, **stats.model_dump())
    return 0


async def _scip_full(args: argparse.Namespace) -> int:
    from server.code.app.index.entry_lock import EntryLockHeld, entry_lock
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    cfg = _repo_cfg(args)
    # scip-full runs scip-clang; an entry another compiler tier owns (Go,
    # Python, TypeScript) is that worker's to re-index, and its checkout is
    # the worker's to fetch. Same ownership test the delta pass applies.
    from server.code.app.index.producer_inference import worker_owns
    from server.code.store.index import CodeRepo

    if not await worker_owns(CodeRepo(), cfg, "clang"):
        producer = cfg.scip_producer or "inferred"
        print(f"skip {ns}: its compiler tier is {producer}, not clang; that tier's worker owns it")
        _note(skipped="not_clang", producer=producer)
        return 0
    # The whole entry runs under its lock, so a delta skips this entry while
    # the checkout moves and the watermark is written; other entries' deltas
    # run on. A delta already on the entry finishes first.
    try:
        async with entry_lock(org, ns, wait=False):
            return await _scip_full_locked(args, org, ns, cfg)
    except EntryLockHeld as held:
        print(f"entry lock held by a delta ({held.path}) — waiting…")
    async with entry_lock(org, ns, wait=True):
        return await _scip_full_locked(args, org, ns, cfg)


async def _scip_full_locked(args: argparse.Namespace, org: str, ns: str, cfg: Any) -> int:
    """One entry's full compiler-tier run; the caller holds the entry lock."""
    import json as _json
    import tempfile
    from pathlib import Path

    from server.code.app.index.scip_delta import ScipDelta, apply_delta
    from server.code.store.index import CodeRepo
    from server.code.workers.scip_apply import watermark_key_for
    from server.settings import settings

    repo = CodeRepo()
    git_head = await repo.get_state(org, ns, "git_head")
    if not git_head:
        print("git_head watermark not seeded — run backfill first", file=sys.stderr)
        return 2

    if getattr(args, "apply_delta", ""):
        return await _scip_apply_only(args, repo, org, ns, git_head, cfg)

    def _paths() -> tuple[Path, Path, Path, Path, list[str]]:
        # Per-repo: each branch indexes in its own working tree and every
        # branch of one repo shares the bare clone.
        checkout = Path(args.checkout or cfg.scip_checkout_dir)
        bare = Path(cfg.git_dir).resolve()
        scip_clang = Path(settings.code_scip_clang_path).resolve()
        missing = [str(p) for p in (checkout, bare, scip_clang) if not p.exists()]
        harness = REPO_ROOT / "scripts/scip_harness/run_delta.sh"
        return checkout, bare, scip_clang, harness, missing

    checkout, bare, scip_clang, harness, missing = await asyncio.to_thread(_paths)
    if missing:
        print(f"missing: {', '.join(missing)}", file=sys.stderr)
        return 2
    # The harness runs the repository's CMake: a repository job, under the
    # same sandbox and gate as the delta worker's.
    from server.code.app.index.compiler_tier import SANDBOX_REQUIRED_SKIP, unsandboxed_skip

    if skip := unsandboxed_skip(cfg, "scip-full", runs_repo_code=True):
        print(f"skip {ns}: {skip}", file=sys.stderr)
        _note(skipped="unsandboxed", reason=skip)
        return 1 if skip == SANDBOX_REQUIRED_SKIP else 0

    await _scip_move_checkout(checkout, bare, git_head)

    with tempfile.TemporaryDirectory(prefix="scip_full_") as tmp:
        delta_path = Path(tmp) / "delta.json"
        rc = await _run_full_harness(args, harness, checkout, scip_clang, Path(tmp))
        if rc != 0:
            print(f"harness failed (exit {rc})", file=sys.stderr)
            _note(head=git_head[:12], harness_exit=rc)
            return 1
        if getattr(args, "keep_delta", ""):
            import shutil

            await asyncio.to_thread(shutil.copyfile, delta_path, args.keep_delta)
            print(f"delta kept at {args.keep_delta}")
        print("applying (declaration filter + scope enforcement)…")
        raw = await asyncio.to_thread(delta_path.read_text)
        delta = ScipDelta.model_validate(_json.loads(raw))

    stats = await apply_delta(repo, org, ns, delta)
    key = watermark_key_for(cfg.scip_producer or "clang")
    await repo.set_state(org, ns, key, git_head)
    print(f"applied: {stats.model_dump()}")
    print(f"{key} -> {git_head[:12]}")
    _note(head=git_head[:12], harness_exit=0, **stats.model_dump())
    return 0


async def _run_full_harness(
    args: argparse.Namespace, harness: Any, checkout: Any, scip_clang: Any, out_dir: Any
) -> int:
    """The full-index harness as a repository job (compiler_tier.run_harness);
    it writes `out_dir/delta.json`. Its output is printed when it ends."""
    from pathlib import Path

    from server.code.app.index.compiler_tier import run_harness
    from server.code.app.index.gitrepo import git_config_env
    from server.code.app.index.sandbox import tool_mounts

    env = {
        **git_config_env(),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHON": sys.executable,
        "DELTA_OUT": str(out_dir / "delta.json"),
        "SCIP_CLANG": str(scip_clang),
        "HOME": str(Path.home()),
    }
    if args.jobs:
        env["JOBS"] = str(args.jobs)
    print("scip-clang full index (this is the ~50 min step; output follows at the end)…")
    rc, out = await run_harness(
        harness,
        checkout,
        ["--full", "", "-"],
        env,
        out_dir=out_dir,
        timeout_s=_FULL_HARNESS_TIMEOUT_S,
        readonly=tool_mounts(scip_clang),
    )
    sys.stdout.write(out)
    return rc


async def _entries(args: argparse.Namespace) -> int:
    from server.code.app.index.entry_lock import entry_lock_path
    from server.code.domain.index.repos import configured_repos

    for c in configured_repos():
        lock = entry_lock_path(c.org, c.namespace).resolve()
        print(c.org, c.namespace, c.scip_checkout_dir, lock)
    return 0


async def _scip_delta(args: argparse.Namespace) -> int:
    """The cron's inline `run_pass()` snippet, as a subcommand — so the pass
    gets the CLI's logging, root span and `record_pass` telemetry instead of
    a bare `print`. Output shape matches the snippet (`RESULT: <result>`)."""
    from server.code.workers.scip_delta import run_pass
    from server.core.ch import get_client
    from server.workers import pass_context

    await get_client().connect()
    started = time.monotonic()
    # Bound so the pass's set_namespace() lands on the row.
    ctx = pass_context.begin("code_scip_delta")
    try:
        result = await run_pass()
        await _record_manual_pass(
            "code_scip_delta",
            "",
            ctx.namespace,
            started=started,
            done=result.deltas_applied,
            rows_written=0,
            errors=len(result.errors),
            counters={
                "repos_checked": result.repos_checked,
                "deltas_applied": result.deltas_applied,
                "skipped": len(result.skipped),
                "errors": len(result.errors),
            },
        )
    finally:
        pass_context.clear()
    print(f"RESULT: {result}")
    _note(
        repos_checked=result.repos_checked,
        deltas_applied=result.deltas_applied,
        skipped=len(result.skipped),
        errors=len(result.errors),
    )
    for err in result.errors:
        print(f"  ! {err}", file=sys.stderr)
    return 1 if result.errors else 0


async def _scip_retry(args: argparse.Namespace) -> int:
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    await get_client().connect()
    org, ns = _identity(args)
    repo = CodeRepo()
    batch = await repo.get_scip_batch(org, ns, args.batch_id)
    if batch is None:
        print(f"batch not found: {args.batch_id}", file=sys.stderr)
        return 2
    await repo.upsert_scip_batch(
        org,
        ns,
        args.batch_id,
        commit_sha=str(batch.get("commit_sha") or ""),
        base_sha=str(batch.get("base_sha") or ""),
        total_parts=int(batch.get("total_parts") or 0),
        status="pending",
        producer=str(batch.get("producer") or ""),
        created_at=batch.get("created_at"),
    )
    print(f"batch {args.batch_id} reset to pending")
    return 0
