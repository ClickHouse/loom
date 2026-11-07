"""`loom code scip-*`, `entries` and `entry-lock`: the compiler-tier full and
delta passes run by hand, and failed-batch retry."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import sys
import time
from pathlib import Path
from typing import Any

from server.cli.code_backfill import _record_manual_pass
from server.cli.code_run import _identity, _note, _repo_cfg


async def _scip_apply_only(
    args: argparse.Namespace,
    repo: Any,
    org: str,
    ns: str,
    git_head: str,
) -> int:
    """`scip-full --apply-delta`: apply a delta the harness already produced
    (`--keep-delta`). The apply is idempotent — a re-run key-diffs against
    the live rows and writes only what is missing — so an interrupted first
    apply resumes here without paying the index step again."""
    import json as _json

    from server.code.app.index.entry_lock import check_held
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
    # scip-full runs scip-clang only, so its delta moves the C++ watermark, also
    # where clang is an extra tier beside the entry's own.
    key = watermark_key_for("clang")
    check_held()
    from server.code.app.index.scip_full import mark_done

    await mark_done(repo, org, ns, delta.commit)
    print(f"applied: {stats.model_dump()}")
    print(f"{key} -> {delta.commit[:12]}")
    _note(head=delta.commit[:12], harness_exit=0, **stats.model_dump())
    return 0


async def _scip_full(args: argparse.Namespace) -> int:
    from server.code.app.index.entry_lock import (
        EntryLockHeld,
        EntryLockLost,
        EntryLockUnavailable,
        entry_lock,
    )
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
    except (EntryLockUnavailable, EntryLockLost) as e:
        print(f"entry lock: {e}", file=sys.stderr)
        return 1
    try:
        async with entry_lock(org, ns, wait=True):
            return await _scip_full_locked(args, org, ns, cfg)
    except (EntryLockHeld, EntryLockUnavailable, EntryLockLost) as e:  # waited the 4 h out
        print(f"entry lock: {e}", file=sys.stderr)
        return 1


def _write_tail(out: str) -> None:
    """The last MiB of the harness output, ending its last line."""
    tail = out[-1024 * 1024 :]
    sys.stdout.write(tail if not tail or tail.endswith("\n") else tail + "\n")


async def _scip_full_locked(args: argparse.Namespace, org: str, ns: str, cfg: Any) -> int:
    """One entry's full compiler-tier run; the caller holds the entry lock."""
    from server.code.app.index import scip_full
    from server.code.app.index.compiler_tier import (
        SANDBOX_REQUIRED_SKIP,
        resolve_binary,
        resolve_checkout,
        stale_compdb,
        unsandboxed_skip,
    )
    from server.code.store.index import CodeRepo
    from server.settings import settings

    repo = CodeRepo()
    git_head = await repo.get_state(org, ns, "git_head")
    if not git_head:
        print("git_head watermark not seeded — run backfill first", file=sys.stderr)
        return 2

    if getattr(args, "apply_delta", ""):
        return await _scip_apply_only(args, repo, org, ns, git_head)

    def _paths() -> tuple[Path, Path, Path, list[str]]:
        # Absolute, as every path the sandbox mounts must be: bwrap runs the job
        # in another directory. Without --checkout the tier's own tree, made if missing.
        bare, missing = Path(cfg.git_dir), []
        if args.checkout:
            checkout = Path(args.checkout)
            missing = [str(p) for p in (checkout, bare) if not p.exists()]
        else:
            checkout, bare, why = resolve_checkout(cfg, "clang")
            missing = [why] if why else []
        scip_clang = resolve_binary(settings.code_scip_clang_path)
        if not scip_clang.exists():
            missing.append(str(scip_clang))
        return checkout.resolve(), bare.resolve(), scip_clang.resolve(), missing

    checkout, bare, scip_clang, missing = await asyncio.to_thread(_paths)
    if missing:
        print(f"missing: {', '.join(missing)}", file=sys.stderr)
        return 2
    # The harness runs the repository's CMake: a repository job, under the
    # same sandbox and gate as the delta worker's.
    if skip := unsandboxed_skip(cfg, "scip-full", runs_repo_code=True):
        print(f"skip {ns}: {skip}", file=sys.stderr)
        _note(skipped="unsandboxed", reason=skip)
        return 1 if skip == SANDBOX_REQUIRED_SKIP else 0

    print(f"scip-clang full index at {git_head[:12]} (about an hour on a large tree)…")
    try:
        stats, out = await scip_full.full_index(
            repo,
            cfg,
            checkout,
            bare,
            scip_clang,
            git_head,
            timeout_s=settings.code_scip_full_timeout_seconds,
            jobs=args.jobs or settings.code_scip_clang_jobs,
            keep_delta=getattr(args, "keep_delta", ""),
        )
    except scip_full.FullIndexFailed as e:
        _write_tail(e.tail)
        print(f"harness failed (exit {e.returncode})", file=sys.stderr)
        _note(head=git_head[:12], harness_exit=e.returncode)
        return 1
    _write_tail(out)
    if getattr(args, "keep_delta", ""):
        print(f"delta kept at {args.keep_delta}")
    print(f"applied: {stats.model_dump()}")
    print(f"{scip_full.WATERMARK} -> {git_head[:12]}")
    stale, _ = await stale_compdb(ns, checkout)
    if stale:
        print(stale, file=sys.stderr)
    _note(head=git_head[:12], harness_exit=0, stale_compdb=bool(stale), **stats.model_dump())
    return 0


# `entry-lock` exits with this when another pass holds the entry (EX_TEMPFAIL).
ENTRY_HELD_EXIT = 75
# How long the command gets to exit after SIGTERM before SIGKILL.
_ENTRY_STOP_GRACE_S = 30.0


async def _entry_locked(args: argparse.Namespace) -> int:
    """`loom code entry-lock`: run the command under the entry's lock (this
    host's file and the deployment's lease), non-blocking. ENTRY_HELD_EXIT when
    another pass holds the entry, 3 when the lease is lost mid-run (the command's
    process group is killed then)."""
    from server.code.app.index.entry_lock import (
        HOLDER_ENV,
        EntryLockHeld,
        EntryLockLost,
        current_holder,
        entry_lock,
    )

    command = args.command[1:] if args.command[:1] == ["--"] else list(args.command)
    if not command:
        print("entry-lock: no command after --", file=sys.stderr)
        return 2
    org, ns = _identity(args)
    try:
        async with entry_lock(org, ns, wait=False):
            # Its uploads name the holder, so the server refuses them once the lease moved.
            return await _run_while_held(command, env={HOLDER_ENV: current_holder()})
    except EntryLockHeld:
        print(f"entry-lock {org}/{ns}: held by another pass")
        return ENTRY_HELD_EXIT
    except EntryLockLost:
        print(f"entry-lock {org}/{ns}: lease lost; the command was killed", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — a lock that could not be asked is a failed step, not a skip
        print(f"entry-lock {org}/{ns}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


async def _run_while_held(command: list[str], *, env: dict[str, str] | None = None) -> int:
    """Run `command` in its own process group to its end. A cancellation (the lease
    was lost) kills the whole group at once; SIGTERM to this process (a caller's
    timeout) sends the group SIGTERM, then SIGKILL after _ENTRY_STOP_GRACE_S."""
    import os
    import signal

    from server.code.app.index.gitrepo import communicate_or_kill

    proc = await asyncio.create_subprocess_exec(
        *command, start_new_session=True, env={**os.environ, **(env or {})}
    )
    run = asyncio.ensure_future(communicate_or_kill(proc, timeout_s=math.inf, group=True))
    term = asyncio.Event()
    with contextlib.suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, term.set)
    stopping = asyncio.ensure_future(term.wait())
    try:
        await asyncio.wait({run, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if not run.done():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait({run}, timeout=_ENTRY_STOP_GRACE_S)
    finally:
        stopping.cancel()
        run.cancel()  # still running: communicate_or_kill SIGKILLs the group
        await asyncio.gather(run, return_exceptions=True)
    return int(proc.returncode or 0)


async def _entries(args: argparse.Namespace) -> int:
    from server.code.app.index.entry_lock import entry_lock_path
    from server.code.domain.index.repos import configured_repos

    for c in configured_repos():
        lock = entry_lock_path(c.org, c.namespace).resolve()
        print(c.org, c.namespace, c.scip_checkout_dir, lock)
    return 0


async def _scip_delta(args: argparse.Namespace) -> int:
    """One code_scip_delta pass as a subcommand, with the CLI's logging, root
    span and `record_pass` telemetry; prints `RESULT: <result>`."""
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
