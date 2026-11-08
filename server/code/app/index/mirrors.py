"""This host's git mirrors.

Every serving process keeps the mirrors its history reads use; the mirrors are a
cache of the remotes, rebuilt on any host from what ClickHouse names. `start_warmup`
(boot) clones the configured, indexed entries' mirrors this host lacks, a few at a
time after a random wait, then runs the host loop. The leader's pass does the
discovery (`ls-remote` of the branches and PR heads) and leaves what it fetched in
ClickHouse: each entry's branch tip (`clone_head`), the open PRs' heads
(`code_gh_items.head_sha`) and the ledger's merges. Each round of the host loop
fetches, by name, only the refs this mirror holds at another sha, so a current host
makes no network call. It also clones entries added since, runs `git gc` when due and
sweeps the mirrors, checkouts and lock files no active entry names. One process per
host does each mirror's round; every fetch into a mirror holds its fetch lock.

`history_dir` is the one gate a history read goes through: the mirror when it is
here, else the `no_git_mirror: …` answer the read degrades to.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import random
import shutil
import time
from collections.abc import AsyncIterator
from typing import Any

from server.code.app.index import gitrepo, pass_fetch, repo_registry
from server.code.app.index.bootstrap import (
    clone_and_pin,
    history_enabled,
    holds,
    job_state,
    mirror_job,
    mirror_ready,
    network_refusal,
    restrict_if_restricted,
)
from server.code.app.index.mirror_locks import fetch_lock_path, partial_mirror, try_lock
from server.code.domain.index import repos
from server.code.store import index as code_store
from server.core import readiness
from server.core.background import detached_task
from server.core.ch import set_query_tag
from server.observability.budgets import request_time_left_s
from server.settings import settings

logger = logging.getLogger("loom.code_index.mirrors")

NO_GIT_HISTORY = "no_git_history: this host keeps no git mirrors (LOOM_GIT_HISTORY=none)"
# The random wait before each warm-up clone, so replicas booting together spread out.
_JITTER_S = 30.0
# A mirror, checkout or lock file younger than this is never swept (an entry added a
# moment ago).
_SWEEP_MIN_AGE_S = 600.0
_GC_MARK = "loom-gc-at"
_LOCK_SUFFIXES = (".clone-lock", ".fetch-lock", ".gc-lock")

# git_dirs the warm-up will clone and has not started yet.
_QUEUED: set[str] = set()
# Set once the warm-up has queued its clones: until then `_QUEUED` says nothing.
_LISTED = asyncio.Event()
# (git_dir, namespace) → the PR number the host's ledger-merge sweep continues below.
_MERGE_BELOW: dict[tuple[str, str], int] = {}
_TASK: asyncio.Task[None] | None = None


def _groups(cfgs: list[Any]) -> dict[str, list[Any]]:
    """Entries by mirror: the branches of one repository share it."""
    out: dict[str, list[Any]] = {}
    for cfg in cfgs:
        if cfg.git_dir:
            out.setdefault(cfg.git_dir, []).append(cfg)
    return out


def _absent_reason(cfg: Any) -> str:
    """Why this host will not clone `cfg`'s mirror for a read; '' when it will."""
    if not cfg.git_remote:
        return "names no remote to clone"
    if reason := network_refusal(cfg):
        return reason
    if not settings.code_history_clone_on_read:
        return "not on this host (code_history_clone_on_read off)"
    return ""


def _present(cfg: Any) -> bool:
    return bool(cfg.git_dir) and os.path.isdir(cfg.git_dir)


async def present_dir(cfg: Any) -> str:
    """`cfg`'s mirror when it is on this host, else ''. Never clones: for status
    reads, which fall back to what the leader published."""
    if cfg is None or not history_enabled():
        return ""
    return cfg.git_dir if await asyncio.to_thread(_present, cfg) else ""


async def history_dir(cfg: Any, *, op: str = "", head: str = "") -> tuple[str, str]:
    """(git_dir, '') when this host's mirror of `cfg` can serve a history read, else
    ('', what the read answers instead). A missing mirror's clone starts here; with
    `op` the read waits for it up to `code_mirror_clone_wait_s` or what is left of the op's
    budget. A present mirror that lacks `head` serves as it is and fetches its branch
    in the background."""
    if cfg is None:
        return "", ""
    if not history_enabled():
        return "", NO_GIT_HISTORY
    if await asyncio.to_thread(_present, cfg):
        if head:
            await holds(cfg, head)
        return cfg.git_dir, ""
    if why := _absent_reason(cfg):
        return "", f"no_git_mirror: {cfg.label} {why}"
    wait_s = min(settings.code_mirror_clone_wait_s, request_time_left_s(op)) if op else 0.0
    if await mirror_ready(cfg, wait_s=wait_s):
        return cfg.git_dir, ""
    state, reason = job_state(cfg.git_dir)
    if state == "failed":
        return "", f"no_git_mirror: {cfg.label}: clone failed ({reason}); retried each minute"
    return "", f"no_git_mirror: cloning {cfg.label}"


def mirror_status() -> dict[str, Any]:
    """This host's mirrors by entry label: `ready`, `cloning` (running or queued),
    `failed` (the last clone's reason), `absent` (not cloned here, and why), and the
    ledger merges a ready mirror could not pin (its remote no longer serves them).
    Reads the disk: an async caller runs it in a thread."""
    if not history_enabled():
        return {"history": "none"}
    out: dict[str, Any] = {
        "ready": [],
        "cloning": [],
        "failed": {},
        "absent": {},
        "unpinned_merges": 0,
    }
    for git_dir, cfgs in _groups(repos.configured_repos()).items():
        label = cfgs[0].label
        if os.path.isdir(git_dir):
            out["ready"].append(label)
            out["unpinned_merges"] += len(pass_fetch.unpinned(git_dir))
            continue
        state, reason = job_state(git_dir)
        if state == "cloning" or git_dir in _QUEUED:
            out["cloning"].append(label)
        elif state == "failed":
            out["failed"][label] = reason
        else:
            out["absent"][label] = _absent_reason(cfgs[0]) or "not asked for on this host yet"
    return out


def _warms_up() -> bool:
    return bool(settings.code_intel_enabled and history_enabled() and settings.code_mirror_warmup)


async def readiness_check() -> readiness.CheckResult:
    """Ready once the warm-up has queued its clones and no clone is running or
    queued here. A failed clone does not count: its remote fails alike on every
    replica, and doctor names it. With the warm-up off nothing is queued at boot,
    so this waits only for the clones reads start."""
    if _warms_up() and not _LISTED.is_set():
        return readiness.result(False, "mirror warm-up has not listed its clones yet")
    status = await asyncio.to_thread(mirror_status)  # walks the mirror directories
    cloning = status.get("cloning") or []
    return readiness.result(not cloning, "mirrors cloning: " + ", ".join(cloning)[:300])


# ---- warm-up and the host loop ------------------------------------------------


def start_warmup() -> asyncio.Task[None] | None:
    """Start this process's mirror warm-up and host loop (boot, every serving
    process); None when there is nothing to keep (code intel or history off)."""
    global _TASK
    if not (settings.code_intel_enabled and history_enabled()):
        return None
    if _TASK is None or _TASK.done():
        _TASK = detached_task(_run(), name="loom-mirror-warmup")
    return _TASK


def stop_warmup() -> None:
    if _TASK is not None:
        _TASK.cancel()


async def _run() -> None:
    set_query_tag("code.mirror_warmup")
    while settings.code_mirror_warmup:
        try:
            await warm_up()
            break
        except Exception as e:  # noqa: BLE001 — retried; a read still clones what it asks for
            logger.warning(
                "mirror warm-up failed, retrying in %.0f s: %s",
                settings.code_mirror_warmup_retry_s,
                e,
            )
            await asyncio.sleep(settings.code_mirror_warmup_retry_s)
    interval = float(settings.code_mirror_fetch_interval_s)
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval * random.uniform(0.9, 1.1))
        try:
            await host_round()
        except Exception as e:  # noqa: BLE001 — the next round retries
            logger.warning("mirror host round failed: %s", e)


async def _clone_candidates() -> list[Any]:
    """One entry per mirror this host lacks and may clone, of entries already indexed
    (an entry still building is the leader's to clone; nothing reads its history)."""
    await repo_registry.refresh_db_repos()
    repo, out = code_store.CodeRepo(), []
    for git_dir, cfgs in _groups(repos.configured_repos()).items():
        cfg = cfgs[0]
        if await asyncio.to_thread(os.path.isdir, git_dir) or _absent_reason(cfg):
            continue
        heads = [str(await repo.get_state(c.org, c.namespace, "git_head") or "") for c in cfgs]
        if any(heads):
            out.append(cfg)
    return out


async def warm_up() -> None:
    """Clone every mirror `_clone_candidates` names, `code_mirror_warmup_concurrency`
    at a time, each after a random wait."""
    todo = await _clone_candidates()
    _QUEUED.update(c.git_dir for c in todo)
    _LISTED.set()
    gate = asyncio.Semaphore(max(1, int(settings.code_mirror_warmup_concurrency)))
    wait_s = float(settings.code_bootstrap_clone_timeout_s) + 60.0

    async def _one(cfg: Any) -> None:
        async with gate:
            await asyncio.sleep(random.uniform(0, _JITTER_S))
            _QUEUED.discard(cfg.git_dir)
            await mirror_job("clone", clone_and_pin, cfg, wait_s)

    try:
        await asyncio.gather(*(_one(c) for c in todo))
    finally:
        _QUEUED.difference_update(c.git_dir for c in todo)
    if todo:
        logger.info("mirror warm-up: %d mirrors asked for", len(todo))


async def host_round() -> None:
    """One round of the host loop: clone what was added (within the concurrency),
    fetch and gc each present mirror whose turn it is, then sweep."""
    room = max(1, int(settings.code_mirror_warmup_concurrency))
    for cfg in await _clone_candidates():
        if room <= 0:
            break
        room -= 1
        await mirror_job("clone", clone_and_pin, cfg, 0)
    for git_dir, cfgs in _groups(repos.configured_repos()).items():
        if not await asyncio.to_thread(os.path.isdir, git_dir):
            continue
        for cfg in cfgs:
            await restrict_if_restricted(cfg)
        async with _host_lock(fetch_lock_path(git_dir)) as held:
            if held:
                await _fetch_round(git_dir, cfgs)
        async with _host_lock(f"{git_dir}.gc-lock") as held:
            if held:
                await gc_if_due(git_dir)
    await sweep()


@contextlib.asynccontextmanager
async def _host_lock(path: str) -> AsyncIterator[bool]:
    """A non-blocking whole-file lock: True when this process holds it."""
    fd = await asyncio.to_thread(try_lock, path)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _mark_age_s(git_dir: str, mark: str) -> float:
    try:
        return time.time() - os.path.getmtime(os.path.join(git_dir, mark))
    except OSError:
        return float("inf")


def _touch(git_dir: str, mark: str) -> None:
    with contextlib.suppress(OSError), open(os.path.join(git_dir, mark), "w"):
        pass


async def _fetch_round(git_dir: str, cfgs: list[Any]) -> None:
    """Bring this mirror to what the leader's pass last fetched (see the module
    docstring), then one page of each entry's ledger merges. Skipped when a process
    of the host, the pass included, fetched it this interval."""
    cfgs = [c for c in cfgs if c.git_remote and not network_refusal(c)]
    interval = float(settings.code_mirror_fetch_interval_s)
    if not cfgs or _mark_age_s(git_dir, pass_fetch.FETCHED_MARK) < interval * 0.8:
        return
    first, repo = cfgs[0], code_store.CodeRepo()
    remote, token = first.git_remote, first.github_token
    try:
        branches, heads = await _published_refs(repo, cfgs)
        local = await gitrepo.local_branch_shas(git_dir)
        stale = [b for b, sha in branches.items() if local.get(b) != sha]
        await gitrepo.fetch_many(git_dir, remote, stale, token=token)
    except Exception as e:  # noqa: BLE001 — a remote or ClickHouse failure: next round
        logger.info("mirror %s: fetch failed: %s", git_dir, e)
        return
    try:
        local_heads = await gitrepo.local_pull_heads(git_dir)
        behind = [n for n, sha in heads.items() if local_heads.get(n) != sha]
        batch = behind[: max(1, int(settings.code_git_pr_head_batch))]
        await gitrepo.fetch_pull_heads(git_dir, remote, batch, token=token)
    except Exception as e:  # noqa: BLE001 — a PR head the remote dropped: the branches still count
        logger.info("mirror %s: PR head fetch failed: %s", git_dir, e)
    await asyncio.to_thread(pass_fetch.mark_fetched, git_dir)
    for cfg in cfgs:
        await _merge_page(cfg, git_dir, repo)


async def _published_refs(repo: Any, cfgs: list[Any]) -> tuple[dict[str, str], dict[int, str]]:
    """(branch → tip, PR number → head) the leader's pass fetched for these entries."""
    branches: dict[str, str] = {}
    heads: dict[int, str] = {}
    for cfg in cfgs:
        if tip := await repo.get_state(cfg.org, cfg.namespace, "clone_head"):
            branches[cfg.git_branch] = tip
        if settings.code_git_fetch_pr_heads and cfg.git_fetch_pr_heads:
            heads.update(await repo.open_pr_heads(cfg.org, cfg.namespace))
    return branches, heads


async def _merge_page(cfg: Any, git_dir: str, repo: Any) -> None:
    """One page of the entry's ledger merges, those the mirror lacks fetched and pinned,
    with this host's own cursor (the leader's lives in ClickHouse)."""
    if not settings.code_git_fetch_ledger_merges:
        return
    key = (git_dir, cfg.namespace)
    below = _MERGE_BELOW.get(key, 0)
    page = max(1, int(settings.code_git_ledger_merge_page))
    batch = max(1, int(settings.code_git_ledger_merge_batch))
    try:
        rows = await repo.merge_shas_page(cfg.org, cfg.namespace, below_pr=below, limit=page)
        if not rows:
            _MERGE_BELOW[key] = 0
            return
        gone = pass_fetch.unpinned(git_dir)
        missing = [
            s for s in await gitrepo.missing_commits(git_dir, [s for _, s in rows]) if s not in gone
        ]
        await pass_fetch.pin_merges(cfg, git_dir, missing[:batch])
        _MERGE_BELOW[key] = rows[-1][0] if len(missing) <= batch else below
    except Exception as e:  # noqa: BLE001 — a ledger or remote failure: next round
        logger.info("mirror %s: ledger merges of %s: %s", git_dir, cfg.namespace, e)


async def gc_if_due(git_dir: str) -> None:
    """`git gc --no-prune` of a mirror or a compiler tier's checkout every
    `code_mirror_gc_interval_h`, at low CPU and IO priority, only when the disk holds
    a second copy plus the margin (the new pack is written beside the old one);
    without that room it waits another interval. Nothing unreachable is pruned, so a
    checkout borrowing objects from the mirror never loses one."""
    hours = float(settings.code_mirror_gc_interval_h)
    if hours <= 0 or _mark_age_s(git_dir, _GC_MARK) < hours * 3600:
        return
    size = await asyncio.to_thread(_tree_bytes, git_dir)
    free = (await asyncio.to_thread(shutil.disk_usage, git_dir)).free
    await asyncio.to_thread(_touch, git_dir, _GC_MARK)
    if free < size + float(settings.code_mirror_gc_margin_gb) * 1024**3:
        logger.warning(
            "mirror %s: gc skipped, %d GB free for a %d GB mirror",
            git_dir,
            free // 1024**3,
            size // 1024**3,
        )
        return
    argv = ["nice", "-n", "19", "git", "--git-dir", git_dir, "-c", "gc.auto=0", "gc"]
    proc = await asyncio.create_subprocess_exec(
        *argv, "--quiet", "--no-prune", stdout=asyncio.subprocess.DEVNULL
    )
    rc = -1
    try:
        rc = await asyncio.wait_for(proc.wait(), settings.code_mirror_gc_timeout_s)
    except TimeoutError:
        pass
    finally:
        if proc.returncode is None:  # timed out, or a cancelled pass: no repack outlives its lock
            proc.kill()
            await proc.wait()
    logger.info("mirror %s: gc exit %s", git_dir, rc)


def _tree_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


# ---- the sweep ----------------------------------------------------------------


async def _named_paths() -> tuple[set[str], set[str]] | None:
    """(mirrors, checkouts) an active entry names; None when that
    is not known for sure (the registry unreadable, the operator's JSON broken), so
    nothing is swept."""
    cfgs = repos.configured_repos()
    if repos.config_error_count():
        return None
    try:
        rows = await code_store.CodeRepo().active_code_repos()
    except Exception as e:  # noqa: BLE001 — unknown registry: sweep nothing
        logger.info("mirror sweep skipped: registry unreadable (%s)", e)
        return None
    mirrors = {c.git_dir for c in cfgs} | {repo_registry.git_dir_for(str(r["repo"])) for r in rows}
    checkouts = {repos.checkout_for(c, p) for c in cfgs for p in ("", *c.scip_extra_producers)}
    checkouts |= {repo_registry.checkout_dir_for(str(r["namespace"])) for r in rows}
    return mirrors, checkouts


async def sweep() -> list[str]:
    """Remove from `code_repos_root` the mirrors, `checkout-*` trees and mirror lock
    files no active entry names (a removed repository, restricted ones included, on
    every host, not only the one `code.repo.remove` reached) and clones a killed
    process left half done. Returns what it removed. One process per host sweeps at
    a time."""
    if not settings.code_self_service_enabled:
        return []
    root = settings.code_repos_root
    if not await asyncio.to_thread(os.path.isdir, root):
        return []
    async with _host_lock(f"{root.rstrip('/')}/.sweep-lock") as held:
        if not held:
            return []
        named = await _named_paths()
        if named is None:
            return []
        return await asyncio.to_thread(_sweep_root, root, *named)


def _sweep_root(root: str, named_mirrors: set[str], named_checkouts: set[str]) -> list[str]:
    root = os.path.abspath(root)
    mirrors = {os.path.abspath(p) for p in named_mirrors if p}
    checkouts = {os.path.abspath(p) for p in named_checkouts if p}
    removed: list[str] = []
    now = time.time()
    for path in sorted(glob.glob(os.path.join(glob.escape(root), "*"))):
        name = os.path.basename(path)
        if now - os.path.getmtime(path) < _SWEEP_MIN_AGE_S:
            continue
        if not os.path.isdir(path):
            if not name.endswith(_LOCK_SUFFIXES):
                continue
            gone = _remove_lock(path, mirrors)
        elif (mirror := partial_mirror(path)) is not None:
            gone = _remove_under_lock(path, f"{mirror}.clone-lock")
        elif name.endswith(".git") and path not in mirrors:
            gone = _remove_under_lock(path, f"{path}.clone-lock")
        elif name.startswith("checkout-") and path not in checkouts:
            shutil.rmtree(path, ignore_errors=True)
            gone = True
        else:
            continue
        if gone:
            removed.append(path)
            logger.info("mirror sweep: removed %s (no active entry names it)", path)
    return removed


def _remove_under_lock(path: str, lock: str) -> bool:
    """Remove `path` while holding `lock` (a clone holds it), False when it is held."""
    fd = try_lock(lock)
    if fd is None:
        return False
    try:
        shutil.rmtree(path, ignore_errors=True)
        return True
    finally:
        os.close(fd)


def _remove_lock(path: str, mirrors: set[str]) -> bool:
    """A lock file of a mirror that is gone and no entry names, while nobody holds it."""
    mirror = path[: path.rindex(".")]
    if mirror in mirrors or os.path.isdir(mirror):
        return False
    fd = try_lock(path)
    if fd is None:
        return False
    try:
        os.unlink(path)
        return True
    finally:
        os.close(fd)


# ---- loom doctor --------------------------------------------------------------


def doctor_checks() -> list[tuple[str, str, str]]:
    """`loom doctor` lines for the operator's entries' mirrors on this host: a git_dir
    that is missing (the worker would clone the remote there, gigabytes for a large
    repository: a mistyped path is caught here) and ledger merges a mirror could not
    pin. (name, PASS | WARN, detail)."""
    if not settings.code_intel_enabled:
        return []
    missing, gone, ready = [], 0, 0
    for git_dir, cfgs in _groups(repos.configured_repos()).items():
        if os.path.isdir(git_dir):
            ready += 1
            gone += len(pass_fetch.unpinned(git_dir))
        elif cfgs[0].source != "registry":
            missing.append(f"{cfgs[0].label}: {os.path.abspath(git_dir)}")
    out = [
        (
            "code_mirrors",
            "WARN" if missing else "PASS",
            (
                "no mirror at "
                + "; ".join(missing)
                + " — the ingest worker clones the remote there (check the path)"
            )
            if missing
            else f"{ready} mirrors present",
        )
    ]
    if gone:
        out.append(
            (
                "code_mirror_unpinned_merges",
                "WARN",
                f"{gone} ledger merge commits this host could not fetch: the remote no "
                "longer serves them; history reads of those PRs answer without the merge",
            )
        )
    return out


__all__ = [
    "NO_GIT_HISTORY",
    "doctor_checks",
    "history_dir",
    "host_round",
    "mirror_status",
    "present_dir",
    "readiness_check",
    "start_warmup",
    "stop_warmup",
    "sweep",
    "warm_up",
]
