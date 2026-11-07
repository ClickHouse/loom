"""The pass every compiler tier runs, and the plumbing the tiers share.

A compiler-tier worker (`server/code/workers/scip_{delta,ts_delta,py_delta,go_delta}.py`)
owns only its producer's `_run_repo`: which harness it runs, with what environment,
and the outcome counters it adds. The loop over configured entries, the ownership
test, failure isolation, the watermark reset after a vanished base commit, the
checkout / bare-clone resolution and the git and harness helpers live here.

House rule: the server never parses protobuf. Every indexer and the protobuf → JSON
conversion run in subprocesses; the tiers consume the JSON delta.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from server.code.app.index import tier_health
from server.code.app.index.entry_lock import EntryLockHeld, EntryLockLost, check_held, entry_lock
from server.code.app.index.gitrepo import is_local_remote, upload_pack_args
from server.code.app.index.producer_inference import worker_owns
from server.code.app.index.sandbox import run_job, sandbox_available
from server.code.domain.index.repos import checkout_for
from server.settings import settings
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
    run_repo: Callable[[CodeRepo, RepoConfig, R], Awaitable[bool | None]],
    *,
    entry_locked: bool = False,
    runs_repo_code: bool = False,
    gc_checkout: bool = False,
    extras: Callable[[R], dict[str, Any]] | None = None,
    health_tier: str = "",
) -> R:
    """One pass of a compiler tier over every configured entry it owns.

    `run_repo(repo, cfg, result)` does one entry's work. A `BaseUnknown` clears the
    tier's `watermark` so the next run indexes in full; any other failure is isolated
    to its entry, recorded in `result.errors` and noted on the pass. With
    `entry_locked` the entry's lock is taken without waiting: a full re-index moving
    the checkout makes the entry a skip, not an error. `runs_repo_code` marks a tier
    that executes the repository's own build (C++ CMake configure): a self-registered
    entry gets it only inside the sandbox. Without the sandbox a self-registered
    entry with a network remote gets no tier at all (`unsandboxed_skip`). With
    `gc_checkout` each entry's checkout is repacked when due (`gc_if_due`) after its
    work, under the entry lock where the tier takes it; only this tier's worker moves
    that tree. Each entry's outcome is its `health_tier` record (default: `producer`;
    `tier_health.record`): a failure, or a success unless `run_repo` returned False (it
    attempted nothing: the entry was not due) or added a skip."""
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo

    tier = health_tier or producer

    async def run_entry(repo: CodeRepo, cfg: RepoConfig) -> None:
        # The watermark reset is one of the entry's writes: under its lock too.
        skips = len(result.skipped)
        try:
            attempted = await run_repo(repo, cfg, result)
        except BaseUnknown as e:
            # Not a failure to report: the next run indexes in full.
            await restart_full(repo, cfg, watermark, e, result.errors)
        else:
            if attempted is not False and len(result.skipped) == skips:
                await tier_health.record(repo, cfg.org, cfg.namespace, tier)
        if gc_checkout:
            await _gc_checkout(cfg, producer)

    # Recorded so /v1/admin/workers.status shows last_run_at / outcome and a
    # stalled tier is diagnosable.
    with record_pass(worker) as rec:
        repo = CodeRepo()
        for cfg in configured_repos():
            if not await worker_owns(repo, cfg, producer):
                continue
            if skip := unsandboxed_skip(cfg, worker, runs_repo_code=runs_repo_code):
                result.skipped.append(f"{cfg.namespace}: {skip}")
                continue
            result.repos_checked += 1
            pass_context.set_namespace(cfg.namespace)  # Where a cut pass stopped
            pass_context.check_stop()
            try:
                if entry_locked:
                    async with entry_lock(cfg.org, cfg.namespace, wait=False):
                        await run_entry(repo, cfg)
                else:
                    await run_entry(repo, cfg)
            except EntryLockHeld as held:
                reason = f"{cfg.namespace}: entry lock held (full pass running)"
                logger.warning("%s: %s — skipped (%s)", worker, reason, held.path)
                result.skipped.append(reason)
            except Exception as e:
                logger.exception("%s: %s failed", worker, cfg.namespace)
                result.errors.append(f"{cfg.namespace}: {type(e).__name__}: {e}")
                pass_context.note_failure(e, cfg.namespace)
                if not isinstance(e, EntryLockLost):  # the entry is another host's now
                    await tier_health.record(repo, cfg.org, cfg.namespace, tier, e)
        log_pass_errors(logger, worker, result.errors)
        rec.outcome = tier_outcome(result, **(extras(result) if extras else {}))
    return result


async def _gc_checkout(cfg: RepoConfig, producer: str) -> None:
    import asyncio

    from server.code.app.index.mirrors import gc_if_due

    git_dir = Path(checkout_for(cfg, producer)) / ".git"
    if await asyncio.to_thread(git_dir.is_dir):
        await gc_if_due(str(git_dir))


UNSANDBOXED_SKIP = "self-registered repository, no build tier without the sandbox"
UNSANDBOXED_NETWORK_SKIP = (
    "self-registered repository with a network remote, no clone, fetch or tier "
    "without the sandbox (nothing would vet the hosts it connects to)"
)
SANDBOX_REQUIRED_SKIP = (
    "LOOM_CODE_SANDBOX_REQUIRED is set and the sandbox cannot run here: no clone, fetch or tier"
)
_unsandboxed_logged: set[tuple[str, str, str]] = set()


def unsandboxed_skip(cfg: RepoConfig, worker: str, *, runs_repo_code: bool = False) -> str:
    """Why `worker` must not run this entry's repository jobs, '' when it may.
    Only a host without the sandbox refuses, and only an entry a viewer added
    from the Repositories page (`source == "registry"`): a job running its own
    build, or any job of an entry whose remote is reached over the network,
    since the plain-process fallback has no egress proxy. An operator's entry,
    and a self-registered local mirror's non-build jobs, run either way, unless
    LOOM_CODE_SANDBOX_REQUIRED says every entry waits for the sandbox.
    Logged once per entry and worker."""
    required = settings.code_sandbox_required
    if (cfg.source != "registry" and not required) or sandbox_available():
        return ""
    if required:
        reason = SANDBOX_REQUIRED_SKIP
    elif runs_repo_code:
        reason = UNSANDBOXED_SKIP
    elif not is_local_remote(cfg.git_remote):
        reason = UNSANDBOXED_NETWORK_SKIP
    else:
        return ""
    key = (worker, cfg.org, cfg.namespace)
    if key not in _unsandboxed_logged:
        _unsandboxed_logged.add(key)
        logger.warning("%s: %s/%s skipped: %s", worker, cfg.org, cfg.namespace, reason)
    return reason


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
    if not checkout.exists():
        # The checkout follows the lease: a new leader (or a new pod) starts from an
        # empty repository, and the tier fetches the target from the mirror. The C++
        # harness configures its build tree on the first run.
        from server.code.app.index.gitrepo import auth_env

        init = subprocess.run(
            ["git", "init", "-q", str(checkout)], capture_output=True, text=True, env=auth_env()
        )
        if init.returncode:
            reason = init.stderr.strip()
            return checkout, bare, f"checkout missing at {checkout} (git init: {reason})"
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
    check_held()
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


# The ref a checkout's fetched target lands on. A fetch negotiates from the
# checkout's refs, and the detached HEAD a pass checks out is not one: with only
# a weeks-old branch tip to offer, every fetch re-sent each object since it, a
# ~2 GB pack every pass on a ClickHouse checkout.
SCIP_HEAD_REF = "refs/loom/scip-head"


def checkout_fetch_args(bare: Path | str, target: str) -> list[str]:
    """`git` arguments that fetch `target` from the local bare clone into a
    checkout: no submodule recursion, and the commit recorded on
    `SCIP_HEAD_REF` so the next fetch sends only what is new."""
    return [
        "fetch",
        "--recurse-submodules=no",
        *upload_pack_args(str(bare)),
        str(bare),
        f"+{target}:{SCIP_HEAD_REF}",
    ]


async def git(
    cwd: Path,
    *args: str,
    timeout_s: float = 600.0,
    retries: int = 0,
    local_remote: Path | None = None,
    network: bool = False,
    token: str = "",
    remote: str = "",
) -> None:
    """Run one git command in a checkout, as a repository job: the checkout writable,
    `local_remote` (the bare clone a fetch reads) read-only, the network only with
    `network`. `token` authenticates to the owner of `remote` (the repository's own
    https remote), so a private submodule of that owner can be fetched. On timeout,
    name the command (a bare TimeoutError says nothing) and retry `retries` times — a
    stall is usually transient contention on the shared bare clone."""
    from server.code.app.index.gitrepo import auth_env

    # The file transport follows the bare clone alone, never the entry's remote.
    if token and remote.startswith(("https://", "http://")):
        env = auth_env(token, remote)
    else:
        env = auth_env(remote=str(local_remote or ""))

    attempt = 0
    while True:
        try:
            done = await run_job(
                # never a repack inside a fetch: the tier's own gc does it (`gc_checkout`)
                ["git", "-C", str(cwd), "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
                env=env,
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
