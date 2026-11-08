"""First index of a self-service repository.

The ingest worker calls these from its per-repository pass for a registry
entry (``cfg.source == "registry"``): clone the bare mirror when it is
missing, then build the initial index a slice at a time (``code_bootstrap_
files_per_pass``) with a cursor in code_index_state, so a restart resumes
and one large repository never monopolises a pass. Progress is written as
``bootstrap_stage`` (queued → cloning → indexing → ready | error) with a
JSON detail the Repositories card renders.

A repository is ready when it is searchable. A slice embeds inline when a real embedder
is up (fake provider → stubs; a slice whose every embed failed opens a process-local
breaker for ten minutes so the bootstrap never waits on a dead provider); the last slice
moves the entry to ``embedding`` until the vector leg covers
``code_ready_embed_coverage`` of the chunks, and only then to ``ready``. The detail
carries an ETA while indexing and the coverage while embedding.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import math
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from server.code.app.index.mirror_locks import (
    fetch_lock,
    lock_file,
    partial_mirror,
    partial_temp_dir,
)
from server.code.app.index.repo_modes import PRIVATE_DIR_MODE, repo_dir_mode, set_repo_dir_mode
from server.code.domain.index.state import (
    CLONE_TIMED_OUT,
    SANDBOX_REQUIRED_REFUSAL,
    STAGE_KEY,
)
from server.core.background import gather_or_cancel
from server.core.errors import error_kind
from server.llm import customer
from server.settings import settings

logger = logging.getLogger("loom.code_index.bootstrap")

CURSOR_KEY = "backfill_cursor:index"
# Stages during which the entry is still being built (the recovery worker
# takes its larger batch, status reads shorten their count cache).
BUILDING_STAGES = frozenset({"queued", "cloning", "indexing", "embedding"})

_embed_breaker_until = 0.0


def history_enabled() -> bool:
    """Whether this host reads git history from its own mirrors
    (`LOOM_GIT_HISTORY`, `none` on an edge host without mirrors)."""
    return settings.git_history != "none"


def embedding_coverage(embeddable: int, embedded: int) -> float:
    """Share of embeddable chunks that carry a vector, 0..1. A namespace with
    no embeddable chunk is fully covered (nothing is waiting)."""
    if embeddable <= 0:
        return 1.0
    return max(0.0, min(1.0, embedded / embeddable))


def is_searchable(coverage: float) -> bool:
    """`ready` means the vector leg answers for at least `code_ready_embed_coverage` of
    the chunks."""
    return coverage >= settings.code_ready_embed_coverage


def eta_text(remaining_files: int, files_per_s: float) -> str:
    """ "~38 min left" from the files still to index and the measured rate;
    "" when there is no rate to divide by (the first slice, a stalled one)."""
    if files_per_s <= 0 or remaining_files <= 0:
        return ""
    minutes = max(1, math.ceil(remaining_files / files_per_s / 60))
    if minutes < 60:
        return f"~{minutes} min left"
    hours, rest = divmod(minutes, 60)
    return f"~{hours} h {rest} min left" if rest else f"~{hours} h left"


def embed_breaker_open(now: float | None = None) -> bool:
    return (now if now is not None else time.monotonic()) < _embed_breaker_until


def open_embed_breaker(now: float | None = None) -> None:
    global _embed_breaker_until
    _embed_breaker_until = (
        now if now is not None else time.monotonic()
    ) + settings.code_bootstrap_embed_breaker_hold_s


def reset_embed_breaker() -> None:
    global _embed_breaker_until
    _embed_breaker_until = 0.0


def bootstrap_embedder() -> Any | None:
    """The embedder a slice embeds with, or None to store stubs: None when
    the configured provider is the fake (a vector of n-gram hashes is worse
    than a stub the recovery worker will fill once a real provider is set)
    and while the breaker is open."""
    from server.embedding.embedder import get_embedder
    from server.embedding.fake import FakeNGramEmbedder

    if embed_breaker_open():
        return None
    embedder = get_embedder()
    if isinstance(embedder, FakeNGramEmbedder):
        return None
    return embedder


async def set_stage(repo: Any, org: str, ns: str, stage: str, **detail: Any) -> None:
    await repo.set_state(org, ns, STAGE_KEY, stage, detail=json.dumps(detail) if detail else "")


async def awaits_first_index(cfg: Any, repo: Any) -> bool:
    """True for a configured entry with no `git_head` yet. Its first index waits for
    `loom code backfill`, and so does every worker that writes the repository's content,
    so the operator can restrict the namespace before anything of it is readable. A
    self-service entry was classified when it was added and builds its own first index."""
    if cfg.source == "registry":
        return False
    return not str(await repo.get_state(cfg.org, cfg.namespace, "git_head") or "")


# Paths whose chmod failed, logged once each: the check runs every pass.
_CHMOD_FAILED: set[str] = set()


def restrict_dir(path: str) -> None:
    """`repo_dir_mode` on one of a restricted entry's directories: other users lose what
    they could read, and in the shared group the group gets back the bits an older
    build's exact 0700 removed. A host that does not hold the directory has nothing to
    change."""
    if not path:
        return
    try:
        st = os.stat(path)
        mode, want = stat.S_IMODE(st.st_mode), repo_dir_mode(st.st_gid)
        # A private directory only needs fixing when its group or others can use it; a
        # setgid bit or owner bits alone give nobody anything.
        if mode != want and (want != PRIVATE_DIR_MODE or mode & 0o077):
            os.chmod(path, want)
            # Taking access away is worth a warning. Adding the group's bits is not, and
            # an older build sharing the root undoes it every round.
            log = logger.warning if mode & ~want & 0o077 else logger.debug
            log(
                "bootstrap: %s set to mode %04o from %04o (the entry is restricted)",
                path,
                want,
                mode,
            )
    except FileNotFoundError:
        return
    except OSError as e:
        if path not in _CHMOD_FAILED:
            _CHMOD_FAILED.add(path)
            logger.warning("bootstrap: could not change the mode of %s: %s", path, e)


def restrict_entry(cfg: Any) -> None:
    """`repo_dir_mode` on the entry's bare mirror and on every compiler-tier checkout it may
    have (`checkout_for` each producer). Sync: callers run it in a thread."""
    from server.code.app.index.producer_inference import PRODUCERS
    from server.code.domain.index.repos import checkout_for

    restrict_dir(cfg.git_dir)
    if cfg.scip_checkout_dir or settings.code_scip_checkout_dir:
        for producer in PRODUCERS:
            restrict_dir(checkout_for(cfg, producer))


async def restrict_if_restricted(cfg: Any) -> None:
    """`restrict_entry` when the entry is restricted by its config, by a maintainer's
    access row or by its directory row alone. Run on every ingest pass and every host's
    mirror round, so a directory created before the restriction gets `repo_dir_mode` on each
    host that holds it. An unreadable classification skips the call: restricting every
    entry on an outage could not be undone, and the next pass retries."""
    from server.auth.directory import ClassificationUnavailable
    from server.identity.app.namespace_classification import is_restricted

    try:
        restricted = await is_restricted(cfg.org, cfg.namespace)
    except ClassificationUnavailable:
        return
    if restricted:
        await asyncio.to_thread(restrict_entry, cfg)


async def clone_if_missing(cfg: Any, repo: Any) -> bool:
    """True when the mirror exists after the call. A failed clone records
    stage=error with the reason and returns False; the next pass retries."""
    if await asyncio.to_thread(os.path.isdir, cfg.git_dir):
        return True
    return not await staged_clone(cfg, repo)


async def staged_clone(cfg: Any, repo: Any) -> str:
    """The leader's clone: `clone_and_pin`, '' on success, else why not. An entry not
    indexed yet shows `cloning` then `error` on its card; an indexed one keeps its
    stage (the mirror is a cache: a new host clones it again), and the path is logged
    so a mistyped operator git_dir is not a silent multi-GB clone."""
    indexed = bool(str(await repo.get_state(cfg.org, cfg.namespace, "git_head") or ""))
    if indexed or cfg.source != "registry":
        logger.warning(
            "bootstrap: %s has no mirror at %s (cwd %s); cloning %s there",
            cfg.label,
            cfg.git_dir,
            Path.cwd(),
            cfg.git_remote,
        )
    if not indexed:
        await set_stage(repo, cfg.org, cfg.namespace, "cloning", remote=cfg.git_remote)
    reason = await clone_and_pin(cfg)
    if reason:
        if not indexed:
            await set_stage(repo, cfg.org, cfg.namespace, "error", reason=reason)
        return reason  # logged by the mirror job
    await repo.set_state(cfg.org, cfg.namespace, "repo_remote", cfg.git_remote)
    await repo.set_state(cfg.org, cfg.namespace, "repo_branch", cfg.git_branch)
    logger.info("bootstrap: cloned %s -> %s", cfg.git_remote, cfg.git_dir)
    return ""


async def clone_in_background(cfg: Any, repo: Any) -> tuple[bool, str]:
    """(mirror present, why the last clone failed). A missing mirror's clone runs as
    the host's mirror job and the pass moves on, so one long clone does not hold up
    every later entry."""
    if await asyncio.to_thread(os.path.isdir, cfg.git_dir):
        return True, ""

    async def _run(c: Any) -> str:
        return await staged_clone(c, repo)

    if await mirror_job("clone", _run, cfg, 0):
        return True, ""
    return False, job_state(cfg.git_dir)[1]


async def clone_mirror(cfg: Any, *, pin: bool = False) -> str:
    """Clone `cfg`'s remote into its bare mirror; '' on success (or when another
    clone got there first), else why not. The processes of one host share the
    mirror directory, so one clones while the others wait on its lock file. With
    `pin`, a clone this call made then pins the ledger merges it lacks."""
    from server.code.app.index.gitrepo import auth_env, remote_access, upload_pack_args
    from server.code.app.index.sandbox import SandboxRequiredError, run_job

    git_dir = cfg.git_dir
    tmp = partial_temp_dir(git_dir)
    lock = -1
    try:
        lock = await lock_file(f"{git_dir}.clone-lock")
        if await asyncio.to_thread(os.path.isdir, git_dir):
            return ""
        # Under the lock no clone is running: what is left was a killed one's.
        for stale in await asyncio.to_thread(glob.glob, f"{glob.escape(git_dir)}.partial-*"):
            mirror = partial_mirror(stale)
            if mirror is not None and os.path.basename(mirror) == os.path.basename(git_dir):
                await asyncio.to_thread(shutil.rmtree, stale, True)
        # Set to `repo_dir_mode` before git writes into it: closed to other users, and in
        # the shared group the other uid can remove it if this clone is killed.
        await asyncio.to_thread(os.makedirs, tmp, PRIVATE_DIR_MODE)
        await asyncio.to_thread(set_repo_dir_mode, tmp)
        # The clone runs as a repository job: the empty target its one writable path.
        done = await run_job(
            ["git", "clone", "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
            + ["--bare", "--quiet", *upload_pack_args(cfg.git_remote), cfg.git_remote, tmp],
            env=auth_env(cfg.github_token, cfg.git_remote),
            timeout_s=settings.code_bootstrap_clone_timeout_s,
            writable=[Path(tmp)],
            **remote_access(cfg.git_remote),
        )
        if done.returncode != 0:
            # The reason reaches `/v1/health?include=mirrors` without a token: git's
            # stderr can name host paths and the remote's URL, so it is logged only.
            stderr = done.stderr.decode(errors="replace").strip()[-600:]
            logger.warning("mirror clone exited %s: %s", done.returncode, stderr)
            return f"git clone failed (exit {done.returncode})"
        # Whatever mode the clone left on the directory, the mirror gets `repo_dir_mode`:
        # closed to other users.
        await asyncio.to_thread(set_repo_dir_mode, tmp)
        await asyncio.to_thread(os.rename, tmp, git_dir)
        if pin:
            await _pin_ledger_merges(cfg)
        return ""
    except TimeoutError:
        return CLONE_TIMED_OUT
    except SandboxRequiredError:
        return SANDBOX_REQUIRED_REFUSAL
    except OSError as e:
        # The reason reaches `code.repos` and `/v1/health?include=mirrors`: the kind; the
        # mirror's path is logged.
        logger.warning("mirror clone failed: %s: %s", type(e).__name__, e)
        return f"clone failed: {error_kind(e)}"
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)
        if lock >= 0:
            os.close(lock)


async def fetch_mirror(cfg: Any) -> str:
    """Bring `cfg`'s mirror up to its remote branch; '' on success, else why not."""
    from server.code.app.index import gitrepo

    try:
        async with fetch_lock(cfg.git_dir):
            await gitrepo.fetch(cfg.git_dir, cfg.git_remote, cfg.git_branch, token=cfg.github_token)
    except gitrepo.GitError as e:
        # As for a clone: git's message is logged, the reason is fixed.
        logger.warning("mirror fetch failed: %s", e)
        return "git fetch failed"
    return ""


async def clone_and_pin(cfg: Any) -> str:
    """A history read's clone: the mirror, then the ledger's merge commits it lacks. A
    fresh clone carries only the branches the remote advertises now, so a merge made on
    a staging branch upstream has since deleted (which the worker's mirror keeps, never
    pruning) is fetched by sha and pinned, as the worker's sweep does."""
    return await clone_mirror(cfg, pin=True)


async def _pin_ledger_merges(cfg: Any) -> None:
    """Every ledger merge of the mirror's entries the clone lacks, fetched by sha a page
    at a time. A failure is logged; the host's fetch loop sweeps the same, slower."""
    from server.code.app.index import gitrepo
    from server.code.app.index.pass_fetch import pin_merges
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo

    if not settings.code_git_fetch_ledger_merges:
        return
    repo, fetched = CodeRepo(), 0
    page = max(1, int(settings.code_git_ledger_merge_page))
    batch = max(1, int(settings.code_git_ledger_merge_batch))
    shared = {
        c.namespace for c in configured_repos() if (c.org, c.git_dir) == (cfg.org, cfg.git_dir)
    }
    for ns in shared | {cfg.namespace}:
        below = 0
        try:
            while rows := await repo.merge_shas_page(cfg.org, ns, below_pr=below, limit=page):
                missing = await gitrepo.missing_commits(cfg.git_dir, [s for _, s in rows])
                async with fetch_lock(cfg.git_dir):
                    for i in range(0, len(missing), batch):
                        fetched += await pin_merges(cfg, cfg.git_dir, missing[i : i + batch])
                below = rows[-1][0]
                if len(rows) < page:
                    break
        except Exception as e:  # noqa: BLE001 — the clone is usable without the pins
            logger.warning(
                "mirror clone of %s: ledger merges of %s not pinned: %s", cfg.label, ns, e
            )
    if fetched:
        logger.info("mirror clone of %s: pinned %d ledger merge commits", cfg.label, fetched)


# The clone or fetch running per (job, git_dir), with when it started: one per mirror
# however many reads ask; after a failure not again for `code_bootstrap_mirror_retry_s`, after a
# success not for `code_bootstrap_mirror_refresh_s`.
_MIRROR_JOBS: dict[tuple[str, str], tuple[asyncio.Task[str], float]] = {}
# Indexed heads a mirror is known to hold (checked once).
_HAVE: set[tuple[str, str]] = set()
# (git_dir, sha) a mirror lacks → (no fetch for it before, fetches made for it). A
# sha no fetch brings waits `code_bootstrap_mirror_retry_s`, doubled per fetch up to
# `code_bootstrap_backoff_max_s`.
_FOREIGN: dict[tuple[str, str], tuple[float, int]] = {}


async def _logged(name: str, run: Any, cfg: Any) -> str:
    """`run(cfg)` under its own query tag, its failure logged once by the job itself,
    not by every read."""
    from server.core.ch import set_query_tag

    set_query_tag(f"code.mirror_{name}")
    try:
        reason = str(await run(cfg))
    except Exception:
        # A raised job's text reaches no reader (`_job_failure` shows its kind): it is
        # logged here, once, and re-raised for the task to hold.
        logger.exception("mirror %s of %s raised", name, cfg.label)
        raise
    if reason:
        logger.warning("mirror %s of %s failed: %s", name, cfg.label, reason)
    return reason


def _job_failure(task: asyncio.Task[str] | None) -> str:
    """Why a finished job failed; '' while it runs, after a success or with no job."""
    if task is None or not task.done():
        return ""
    if task.cancelled():
        return "cancelled"
    if (e := task.exception()) is not None:
        # History reads and `/v1/health` (no token) show it; `_logged` has the text.
        return error_kind(e)
    return task.result()


def _current_job(key: tuple[str, str]) -> asyncio.Task[str] | None:
    """The job running for `key`, or finished within its interval; None when one is due."""
    task, started = _MIRROR_JOBS.get(key, (None, 0.0))
    if task is not None and task.done():
        failed = bool(_job_failure(task))
        if time.monotonic() - started >= (
            settings.code_bootstrap_mirror_retry_s
            if failed
            else settings.code_bootstrap_mirror_refresh_s
        ):
            return None
    return task


def job_state(git_dir: str) -> tuple[str, str]:
    """('cloning' | 'failed' | '', reason) of this process's clone of `git_dir`."""
    task, _ = _MIRROR_JOBS.get(("clone", git_dir), (None, 0.0))
    if task is not None and not task.done():
        return "cloning", ""
    reason = _job_failure(task)
    return ("failed", reason) if reason else ("", "")


async def mirror_job(name: str, run: Any, cfg: Any, wait_s: float) -> bool:
    """Whether the job succeeded. The read that starts it waits up to `wait_s`;
    one that finds it running answers at once, so a clone of minutes costs one
    read the wait, not every read. The job belongs to the process, not to the
    request that started it (a fresh context)."""
    from server.core.background import detached_task

    key = (name, cfg.git_dir)
    task = _current_job(key)
    if task is None:
        task = detached_task(_logged(name, run, cfg), name=f"loom-mirror-{name}:{cfg.namespace}")
        _MIRROR_JOBS[key] = (task, time.monotonic())
    elif not task.done():
        return False
    try:
        return not await asyncio.wait_for(asyncio.shield(task), max(0.0, wait_s))
    except TimeoutError:
        return False
    except Exception:  # noqa: BLE001 — the job's own failure, already logged
        return False


def network_refusal(cfg: Any) -> str:
    """Why this host must not clone or fetch `cfg` for a read, '' when it may: the
    worker's rule (`compiler_tier.unsandboxed_skip`), so a viewer-added remote is
    never reached by a plain process without the egress proxy."""
    from server.code.app.index.compiler_tier import unsandboxed_skip

    return unsandboxed_skip(cfg, "history_read")


async def mirror_ready(
    cfg: Any, *, want: str = "", wait_s: float = 10.0, fetchable: Any = None
) -> bool:
    """Whether this host's mirror of `cfg` holds `want` (any commit when empty) now.

    A host without the mirror clones it from the remote ClickHouse names
    (`code_history_clone_on_read`), the read waiting up to `wait_s` for it. A present
    mirror answers at once: one that lacks `want` fetches the branch in the background
    and answers False meanwhile. `fetchable` (async, no arguments) says whether `want`
    is the branch's to fetch; a commit it refuses (a PR head), or one a fetch did not
    bring, is not fetched for again until its backoff passes."""
    if not history_enabled():
        return False
    if not await asyncio.to_thread(os.path.isdir, cfg.git_dir):
        if not cfg.git_remote or not settings.code_history_clone_on_read:
            return False
        if network_refusal(cfg):
            return False
        return await mirror_job("clone", clone_and_pin, cfg, wait_s)
    return await holds(cfg, want, fetchable=fetchable)


async def holds(cfg: Any, want: str, *, fetchable: Any = None) -> bool:
    """Whether the mirror of `cfg`, present on this host, holds `want` (any commit when
    empty); one that lacks it fetches the branch in the background (see `mirror_ready`)."""
    from server.code.app.index import gitrepo

    key = (cfg.git_dir, want)
    if not want or key in _HAVE:
        return True
    until, tries = _FOREIGN.get(key, (0.0, 0))
    now = time.monotonic()
    if now < until:
        return False
    try:
        await gitrepo.run(cfg.git_dir, "cat-file", "-e", f"{want}^{{commit}}")
    except gitrepo.GitError:
        running = _current_job(("fetch", cfg.git_dir))
        if not cfg.git_remote or (running is not None and not running.done()):
            return False  # nothing to fetch from, or a fetch is under way
        if len(_FOREIGN) > 1024:
            _FOREIGN.clear()
        if network_refusal(cfg) or (fetchable is not None and not await fetchable()):
            _FOREIGN[key] = (now + settings.code_bootstrap_mirror_retry_s, 0)
            return False
        if tries and not until:
            # The fetch made for it ended without it (the remote lost it): back off.
            _FOREIGN[key] = (
                now
                + min(
                    settings.code_bootstrap_mirror_retry_s * 2 ** (tries - 1),
                    settings.code_bootstrap_backoff_max_s,
                ),
                tries,
            )
            return False
        _FOREIGN[key] = (0.0, tries + 1)
        await mirror_job("fetch", fetch_mirror, cfg, 0)
        return False
    _FOREIGN.pop(key, None)
    if len(_HAVE) > 1024:
        _HAVE.clear()
    _HAVE.add(key)
    return True


async def index_slice(
    cfg: Any,
    repo: Any,
    head: str,
    *,
    max_files: int | None = None,
    stop_after: float | None = None,
) -> dict[str, Any]:
    """Index the next ``max_files`` paths of ``head``; on the last slice set the
    git_head watermark and the extractor stamps and mark the entry ready.
    Returns the progress it wrote.

    The slice goes in rounds of `code_ingest_round_files` (the per-file resolution reads
    served round-wide); `stop_after` (a monotonic instant) is checked before every round
    — past it no further round starts and the cursor advances by the paths the started
    rounds took on, so the next pass continues there (`partial` on the progress)."""
    from server.code.app.index import gitrepo
    from server.code.app.index.denylist import load_denylist
    from server.code.app.index.ingest import ingest_file, read_capped
    from server.code.app.index.ingest_headers import headers_first
    from server.embedding.embedder import get_embedder

    org, ns = cfg.org, cfg.namespace
    limit = max_files or settings.code_bootstrap_files_per_pass
    paths = headers_first(await gitrepo.ls_tree(cfg.git_dir, head, cfg.path_prefixes))
    cursor = await repo.get_state(org, ns, CURSOR_KEY)
    start = int(cursor) if cursor.isdigit() else 0
    total = len(paths)
    if start >= total:
        await _finish(repo, org, ns, head, total)
        return {"done": total, "total": total, "stage": "ready"}

    model_name = get_embedder().model_name
    embedder = bootstrap_embedder()
    denylist = await load_denylist(cfg, head, gitrepo.show_file)
    sem = asyncio.Semaphore(4)
    counters = {"files": 0, "errors": 0, "embedded": 0, "embed_stubs": 0}
    slice_paths = paths[start : start + limit]

    async def _source(path: str) -> tuple[str, bytes | None]:
        async with sem:
            source, counter = await read_capped(
                gitrepo.show_file,
                repo,
                org,
                ns,
                git_dir=cfg.git_dir,
                path=path,
                commit_sha=head,
                embedding_model=model_name,
                denylist=denylist,
            )
        if counter:
            counters[counter] += 1
        return path, source

    async def _one(reads: Any, path: str, source: bytes) -> None:
        async with sem:
            try:
                stats = await ingest_file(
                    reads,
                    org,
                    ns,
                    path=path,
                    source=source,
                    commit_sha=head,
                    embedding_model=model_name,
                    embedder=embedder,
                    max_chunk_chars=settings.code_chunk_max_chars,
                    denylist=denylist,
                    parsed=reads.parsed.get(path),
                )
                counters["files"] += 1
                counters["embedded"] += int(getattr(stats, "embedded", 0) or 0)
                counters["embed_stubs"] += int(getattr(stats, "embed_stubs", 0) or 0)
            except customer.HOLDS:
                raise  # the org's model calls are held: the repository waits
            except Exception as e:  # noqa: BLE001 — per-file, keep going
                counters["errors"] += 1
                logger.warning("bootstrap: %s %s: %s", cfg.label, path, e)

    started = time.monotonic()
    attempted, partial = await _slice_rounds(
        repo,
        org,
        ns,
        slice_paths,
        source=_source,
        one=_one,
        denylist=denylist,
        stop_after=stop_after,
    )
    done = start + attempted
    await repo.set_state(org, ns, CURSOR_KEY, str(done))
    rate = counters["files"] / max(time.monotonic() - started, 1e-9)
    _note_embed_outcome(cfg, embedder, counters)
    if done >= total:
        stage = await _finish(repo, org, ns, head, total)
        return {"done": total, "total": total, "stage": stage}
    await set_stage(
        repo,
        org,
        ns,
        "indexing",
        done=done,
        total=total,
        files_per_s=round(rate, 1),
        eta=eta_text(total - done, rate),
        errors=counters["errors"],
        embedded=counters["embedded"],
        embed_stubs=counters["embed_stubs"],
    )
    progress: dict[str, Any] = {"done": done, "total": total, "stage": "indexing"}
    if partial:
        progress["partial"] = True
    return progress


async def _slice_rounds(
    repo: Any,
    org: str,
    ns: str,
    slice_paths: list[str],
    *,
    source: Any,
    one: Any,
    denylist: Any,
    stop_after: float | None,
) -> tuple[int, bool]:
    """The slice in rounds of `code_ingest_round_files`, the budget checked before each;
    returns (paths attempted, cut by the budget)."""
    from server.code.app.index.round_reads import RoundReads, round_size, rounds

    size = round_size()
    attempted, partial = 0, False
    for batch in rounds(slice_paths, size):
        if stop_after is not None and time.monotonic() >= stop_after:
            partial = True
            break
        attempted += len(batch)
        found = dict(await asyncio.gather(*(source(p) for p in batch)))
        sources = {p: src for p, src in found.items() if src is not None}
        async with RoundReads(repo, org, ns) as reads:
            if size > 0:
                await reads.prefetch(sources, denylist)
            # A held file ends the round, its peers cancelled before the reads close.
            await gather_or_cancel(*(one(reads, p, sources[p]) for p in batch if p in sources))
    return attempted, partial


def _note_embed_outcome(cfg: Any, embedder: Any | None, counters: dict[str, int]) -> None:
    """A slice that embedded with a provider and got nothing back opens the
    breaker: the next slices store stubs instead of waiting on it."""
    if embedder is None or counters["embed_stubs"] == 0 or counters["embedded"] > 0:
        return
    open_embed_breaker()
    logger.warning(
        "bootstrap: %s: %d chunks stubbed, none embedded — storing stubs for the next "
        "%d s (the recovery worker fills them)",
        cfg.label,
        counters["embed_stubs"],
        int(settings.code_bootstrap_embed_breaker_hold_s),
    )


async def record_backfill_stamps(repo: Any, org: str, ns: str) -> None:
    """A namespace indexed outside the worker (the first-build slices, the
    `loom code backfill` CLI) has rows produced by THIS extractor; record the
    stamps the way the worker's first observation would, so a read that asks
    "does this namespace's parser tier carry constructor calls" has an answer
    without a worker pass. A record already present — a worker mid-heal — is
    left alone."""
    from server.code.domain.extract.extractor_stamp import STAGES, extractor_stamp, stage_stamps

    state = await repo.all_state(org, ns)
    if str((state.get("extractor_stamp") or {}).get("value") or ""):
        return
    now = int(time.time())
    await repo.set_state(
        org,
        ns,
        "extractor_stamp",
        extractor_stamp(),
        detail=json.dumps({"at": now, "backfill": True}),
    )
    current = stage_stamps()
    for s in STAGES:
        await repo.set_state(
            org,
            ns,
            f"extractor_stamp_{s}",
            current[s],
            detail=json.dumps({"at": now, "from": "", "backfill": True}),
        )


async def _finish(repo: Any, org: str, ns: str, head: str, total: int) -> str:
    """The last slice: watermark, stamps, then `ready` when the vector leg
    already covers the chunks (a slice embedded inline), else `embedding`
    with the coverage; `maybe_finish_embedding` moves it on later."""
    await repo.set_state(org, ns, "git_head", head, detail="bootstrap")
    await repo.set_state(org, ns, CURSOR_KEY, "")
    await record_backfill_stamps(repo, org, ns)
    stage = await _write_embed_stage(repo, org, ns, total=total, head=head[:12])
    logger.info(
        "bootstrap: %s/%s initial index complete at %s (%d files) → %s",
        org,
        ns,
        head[:12],
        total,
        stage,
    )
    return stage


async def _write_embed_stage(repo: Any, org: str, ns: str, **detail: Any) -> str:
    embeddable, embedded = await repo.chunk_embed_coverage(org, ns)
    coverage = embedding_coverage(embeddable, embedded)
    reached = "ready" if is_searchable(coverage) else "embedding"
    await set_stage(
        repo,
        org,
        ns,
        reached,
        chunks=embeddable,
        embedded=embedded,
        embedding_coverage=round(coverage, 4),
        **detail,
    )
    return reached


async def maybe_finish_embedding(cfg: Any, repo: Any) -> str:
    """Called by the ingest pass for a self-service entry whose first index is
    built: while the stage is `embedding`, re-read the coverage and either
    move to `ready` or refresh the percentage the card shows. Returns the
    stage after the call ("" when the entry is not in that stage)."""
    stage = await repo.get_state(cfg.org, cfg.namespace, STAGE_KEY)
    if stage != "embedding":
        return ""
    new_stage = await _write_embed_stage(repo, cfg.org, cfg.namespace)
    if new_stage == "ready":
        logger.info("bootstrap: %s is ready — embeddings cover the index", cfg.label)
    return new_stage
