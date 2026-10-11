"""The pass every compiler tier runs, and the plumbing the tiers share.

A compiler-tier worker (`server/code/workers/scip_{delta,ts_delta,py_delta,go_delta}.py`)
owns only its producer's `_run_repo`: which harness it runs, with what environment,
and the outcome counters it adds. The loop over configured entries, the ownership
test, failure isolation, the watermark reset after a vanished base commit, the
checkout / bare-clone resolution and the subprocess helpers live here.

House rule: the server never parses protobuf. Every indexer and the protobuf → JSON
conversion run in subprocesses; the tiers consume the JSON delta.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from server.code.app.index.entry_lock import EntryLockHeld, entry_lock
from server.code.app.index.producer_inference import worker_owns
from server.code.app.index.sandbox import run_job, sandbox_available
from server.code.domain.index.repos import checkout_for
from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_index.compiler_tier")

# The exit code every tier harness gives when the window's base commit is
# not in the checkout (history rewritten, a squashed or force-pushed branch):
# the window cannot be diffed, and only a full index can bring the tier back.
BASE_UNKNOWN_EXIT = 3


class BaseUnknown(RuntimeError):
    """The window's base commit is gone: the next run must index in full."""


class TierResult(Protocol):
    """What every tier's pass result carries (its own dataclass adds counters)."""

    repos_checked: int
    deltas_applied: int
    skipped: list[str]
    errors: list[str]


async def run_tier[R: TierResult](
    worker: str,
    producer: str,
    watermark: str,
    result: R,
    run_repo: Callable[[CodeRepo, RepoConfig, R], Awaitable[None]],
    *,
    entry_locked: bool = False,
    extras: Callable[[R], dict[str, Any]] | None = None,
) -> R:
    """One pass of a compiler tier over every configured entry it owns.

    `run_repo(repo, cfg, result)` does one entry's work. A `BaseUnknown` clears the
    tier's `watermark` so the next run indexes in full; any other failure is isolated
    to its entry, recorded in `result.errors` and noted on the pass. With
    `entry_locked` the entry's lock is taken without waiting: a full re-index moving
    the checkout makes the entry a skip, not an error."""
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo

    # Recorded so /v1/admin/workers.status shows last_run_at / outcome and a
    # stalled tier is diagnosable without the cron log.
    with record_pass(worker) as rec:
        repo = CodeRepo()
        for cfg in configured_repos():
            if not await worker_owns(repo, cfg, producer):
                continue
            if not may_build(cfg, worker):
                result.skipped.append(f"{cfg.namespace}: {UNSANDBOXED_SKIP}")
                continue
            result.repos_checked += 1
            pass_context.set_namespace(cfg.namespace)  # Where a cut pass stopped
            try:
                if entry_locked:
                    async with entry_lock(cfg.org, cfg.namespace, wait=False):
                        await run_repo(repo, cfg, result)
                else:
                    await run_repo(repo, cfg, result)
            except EntryLockHeld as held:
                reason = f"{cfg.namespace}: entry lock held (full pass running)"
                logger.warning("%s: %s — skipped (%s)", worker, reason, held.path)
                result.skipped.append(reason)
            except BaseUnknown as e:
                await restart_full(repo, cfg, watermark, e, result.errors)
            except Exception as e:
                logger.exception("%s: %s failed", worker, cfg.namespace)
                result.errors.append(f"{cfg.namespace}: {type(e).__name__}: {e}")
                pass_context.note_failure(e, cfg.namespace)
        log_pass_errors(logger, worker, result.errors)
        rec.outcome = tier_outcome(result, **(extras(result) if extras else {}))
    return result


UNSANDBOXED_SKIP = "self-registered repository, no build tier without the sandbox"
_unsandboxed_logged: set[tuple[str, str, str]] = set()


def may_build(cfg: RepoConfig, worker: str) -> bool:
    """Whether a compiler tier may run this entry's build. A tier runs the
    repository's own code (CMake, Go modules, package installs), so an entry a
    viewer added from the Repositories page (`source == "registry"`) gets one
    only inside the sandbox; an operator's entry runs either way. Logged once
    per entry and tier."""
    if cfg.source != "registry" or sandbox_available():
        return True
    key = (worker, cfg.org, cfg.namespace)
    if key not in _unsandboxed_logged:
        _unsandboxed_logged.add(key)
        logger.warning("%s: %s/%s skipped: %s", worker, cfg.org, cfg.namespace, UNSANDBOXED_SKIP)
    return False


def tier_outcome(result: TierResult, **extra: Any) -> dict[str, Any]:
    """A compiler-tier pass's outcome: the counts every tier states, the
    tier's own `extra` counters, and one `reason` — applied, the first skip,
    error (every repository failed or restarted), or current."""
    return {
        "repos_checked": result.repos_checked,
        "deltas_applied": result.deltas_applied,
        **extra,
        "skipped": result.skipped[:5],
        "errors": len(result.errors),
        "reason": (
            "applied"
            if result.deltas_applied
            else result.skipped[0]
            if result.skipped
            else "error"
            if result.errors
            else "current"
        ),
    }


def resolve_checkout(cfg: RepoConfig, producer: str) -> tuple[Path, Path, str | None]:
    """(checkout, bare clone, missing) for `producer` on this entry. Sync: callers run
    it in a thread, the worker convention that keeps the event loop free of filesystem
    calls. The checkout is absolute because a harness hands it to the converter after
    `cd`-ing into it; the bare clone is shared by every branch of the repository."""
    checkout = Path(checkout_for(cfg, producer)).absolute()
    bare = Path(cfg.git_dir)
    if not bare.exists():
        return checkout, bare, f"bare clone missing at {bare}"
    if not checkout.exists() and cfg.source == "registry":
        # a Repositories-page entry has no operator to create its checkout; an
        # empty repository is enough, the tier fetches the target from the mirror
        from server.code.app.index.gitrepo import auth_env

        subprocess.run(
            ["git", "init", "-q", str(checkout)], check=True, capture_output=True, env=auth_env()
        )
    if not checkout.exists():
        return checkout, bare, f"checkout missing at {checkout}"
    return checkout, bare.resolve(), None


def harness_failure(returncode: int | None, tail: str) -> RuntimeError:
    """The error for a harness exit: `BaseUnknown` for `BASE_UNKNOWN_EXIT`."""
    if returncode == BASE_UNKNOWN_EXIT:
        return BaseUnknown(f"harness exit {returncode}: base commit unknown: {tail[-500:]}")
    return RuntimeError(f"harness exit {returncode}: {tail}")


async def restart_full(
    repo: CodeRepo, cfg: RepoConfig, watermark_key: str, err: BaseUnknown, errors: list[str]
) -> None:
    """Clear a tier's watermark after `BaseUnknown`, so its next run is a
    full index (which also retires rows the new index no longer emits),
    instead of retrying a window that can never be diffed."""
    await repo.set_state(
        cfg.org, cfg.namespace, watermark_key, "", detail=f"base unknown: {err}"[:500]
    )
    errors.append(f"{cfg.namespace}: {err}; {watermark_key} cleared, the next run indexes in full")
    logger.warning(
        "%s: %s base unknown, %s cleared for a full run",
        watermark_key,
        cfg.namespace,
        watermark_key,
    )


async def compiler_target(repo: CodeRepo, cfg: RepoConfig) -> str:
    """The commit a compiler tier indexes next, or "" before the entry's first
    index. It is the branch head in the entry's mirror, which `code_ingest`
    fetches every pass: a compiler tier follows the branch, not the parser
    tier's `git_head` watermark, so a parser backlog does not hold it back.
    Before the first index (no `git_head`) it waits. When the mirror cannot
    answer it falls back to `git_head`."""
    from server.code.app.index import gitrepo

    watermark = str(await repo.get_state(cfg.org, cfg.namespace, "git_head") or "")
    if not watermark or not cfg.git_branch:
        return watermark
    try:
        head = await gitrepo.head_sha(cfg.git_dir, cfg.git_branch)
    except (gitrepo.GitError, OSError) as e:
        logger.warning(
            "compiler_target: %s mirror head unreadable (%s); using git_head", cfg.namespace, e
        )
        return watermark
    return head or watermark


def gone_documents(live: set[str], delta: Any, producer: str) -> list[str]:
    """The live compiler-tier paths of `producer`'s languages the full-run delta does
    not mention, joined with the paths it already marks deleted — sorted, so the apply
    is deterministic. Another tier's documents in the same namespace are never gone:
    a full TypeScript run must not retire the Python tier's rows."""
    from server.code.domain.index.compiler_tiers import owns_path

    present = {f.path for f in delta.files}
    mine = {p for p in live if owns_path(producer, p)}
    return sorted((mine - present) | set(delta.deleted_files))


def resolve_binary(configured: str) -> Path:
    """Where an indexer binary is. A bare command name (the default,
    "scip-typescript") is looked up on PATH — `.absolute()` alone would turn
    it into `<cwd>/scip-typescript` and fail with exit 127 although the binary
    is on PATH. A path with a directory part is kept as
    written, `.absolute()` and never `.resolve()`: an npm-installed
    binary is a symlink under node_modules/.bin, and the pnpm shim
    the harness needs sits beside the LINK, not beside its target."""
    import shutil

    if os.sep not in configured:
        found = shutil.which(configured)
        if found:
            return Path(found).absolute()
    return Path(configured).absolute()


async def communicate_or_kill(
    proc: asyncio.subprocess.Process, timeout_s: float, *, group: bool = False
) -> bytes:
    """`proc`'s stdout, or TimeoutError after `timeout_s`. The process is killed
    on any early exit, its own timeout or the pass cancelled (the supervisor's
    deadline is shorter than the harness timeouts; shutdown): otherwise it
    keeps running in the checkout the next pass checks out again.

    `group=True` is for a process started with `start_new_session=True`: the
    kill goes to its whole process group, because a harness is a bash script
    whose compiler and indexer children (scip-clang, node, scip-python) would
    outlive a kill of bash alone. The group is killed even when bash already
    exited, since a child still holding the pipe is what kept the wait open."""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except BaseException:
        if group:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        raise
    return out or b""


# The ref a checkout's fetched target lands on. A fetch negotiates from the
# checkout's refs, and the detached HEAD a pass checks out is not one: with only
# a weeks-old branch tip to offer, every fetch re-sent each object since it, a
# ~2 GB pack every pass on a ClickHouse checkout.
SCIP_HEAD_REF = "refs/loom/scip-head"


def checkout_fetch_args(bare: Path | str, target: str) -> list[str]:
    """`git` arguments that fetch `target` from the local bare clone into a
    checkout: no submodule recursion, and the commit recorded on
    `SCIP_HEAD_REF` so the next fetch sends only what is new."""
    return ["fetch", "--recurse-submodules=no", str(bare), f"+{target}:{SCIP_HEAD_REF}"]


async def git(
    cwd: Path,
    *args: str,
    timeout_s: float = 600.0,
    retries: int = 0,
    local_remote: Path | None = None,
    network: bool = False,
) -> None:
    """Run one git command in a checkout, as a repository job: the checkout writable,
    `local_remote` (the bare clone a fetch reads) read-only, the network only with
    `network`. On timeout, name the command (a bare TimeoutError says nothing) and
    retry `retries` times — a stall is usually transient contention on the shared
    bare clone."""
    from server.code.app.index.gitrepo import auth_env

    attempt = 0
    while True:
        try:
            done = await run_job(
                ["git", "-C", str(cwd), *args],
                env=auth_env(remote=str(local_remote or "")),
                timeout_s=timeout_s,
                writable=[cwd],
                readonly=[local_remote] if local_remote else [],
                network=network,
                merge_stderr=True,
            )
        except TimeoutError:
            attempt += 1
            if attempt > retries:
                raise TimeoutError(
                    f"git {' '.join(args[:2])} in {cwd} exceeded {timeout_s:.0f}s"
                ) from None
            logger.warning(
                "git %s in %s exceeded %.0fs — retry %d/%d",
                args[0],
                cwd,
                timeout_s,
                attempt,
                retries,
            )
            continue
        if done.returncode != 0:
            tail = done.stdout.decode("utf-8", "replace")[-500:]
            raise RuntimeError(f"git {args[0]} failed: {tail}")
        return


async def run_harness(
    harness: Path,
    checkout: Path,
    args: Sequence[str],
    env: dict[str, str],
    *,
    out_dir: Path,
    timeout_s: float,
    writable: Sequence[Path] = (),
    readonly: Sequence[Path] = (),
    network: bool = False,
) -> tuple[int, str]:
    """A tier harness as a repository job: (exit code, output). It runs the
    repository's own build files, so only the checkout, `out_dir` and `writable`
    are writable; the checkout's .git stays read-only (a hook or config planted
    there would run outside the sandbox later), as do the harness scripts.
    RuntimeError naming the timeout past `timeout_s`."""
    from server.core.paths import SCIP_HARNESS_DIR

    try:
        done = await run_job(
            ["bash", str(harness), str(checkout), *args],
            env=env,
            timeout_s=timeout_s,
            writable=[checkout, out_dir, *writable],
            readonly=[checkout / ".git", SCIP_HARNESS_DIR, *readonly],
            network=network,
            merge_stderr=True,
        )
    except TimeoutError:
        raise RuntimeError(f"harness timed out after {timeout_s}s") from None
    return done.returncode, done.stdout.decode("utf-8", "replace")
