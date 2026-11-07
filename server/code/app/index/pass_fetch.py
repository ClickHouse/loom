"""What the ingest pass fetches into a namespace's mirror before it walks: the branch,
extra release branches, merges the ledger names and open PR heads."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from server.code.app.index.mirror_locks import fetch_lock
from server.code.app.index.pass_budget import CodeIngestResult
from server.settings import settings

logger = logging.getLogger("loom.workers.code_ingest")


# Touched in a mirror after each fetch round, the pass's or the host loop's: another
# process of the host skips its own round within the interval.
FETCHED_MARK = "loom-host-fetch-at"


def mark_fetched(git_dir: str) -> None:
    with contextlib.suppress(OSError), open(os.path.join(git_dir, FETCHED_MARK), "w"):
        pass


# When each shared mirror last fetched its extra branches
# (monotonic seconds). Several entries can share one mirror; the first entry
# of a pass fetches, the rest skip within the pass interval.
_EXTRA_FETCHED_AT: dict[str, float] = {}


def _extra_fetch_due(git_dir: str, now: float) -> bool:
    gap = max(30.0, float(settings.code_ingest_interval_seconds) * 0.9)
    return now - _EXTRA_FETCHED_AT.get(git_dir, -1e12) >= gap


async def _publish_clone_head(repo: Any, org: str, ns: str, head: str, branch: str) -> None:
    """`clone_head` state row: value = the fetched tip's sha, detail = the
    branch it is the tip of."""
    try:
        await repo.set_state(org, ns, "clone_head", head, detail=branch)
    except Exception as e:  # noqa: BLE001 — the tip is a convenience for the status page
        logger.debug("code_ingest: clone_head not published for %s: %s", ns, e)


async def fetch_extra_branches(cfg: Any, git_dir: str, result: CodeIngestResult) -> int:
    """The mirror carries every remote branch the entry's globs name
    (`git_extra_branches`): one `ls-remote`, one `fetch
    --stdin`, no `--prune`, once per mirror per pass. A failure is counted
    and logged, never fatal — the configured branch is already fetched and
    the pass indexes it. Returns the number of branches fetched (0 = unset,
    not due, or failed)."""
    from server.code.app.index import gitrepo

    globs = gitrepo.branch_globs(cfg.git_extra_branches)
    if not globs:
        return 0
    now = time.monotonic()
    if not _extra_fetch_due(git_dir, now):
        return 0
    _EXTRA_FETCHED_AT[git_dir] = now  # a failing remote is retried next pass, not next entry
    try:
        remote = await gitrepo.remote_branches(git_dir, cfg.git_remote, token=cfg.github_token)
        local = await gitrepo.local_branch_shas(git_dir)
        wanted = gitrepo.branches_to_fetch(remote, local, globs, skip=cfg.git_branch)
        sent = await gitrepo.fetch_branches(git_dir, cfg.git_remote, wanted, token=cfg.github_token)
    except gitrepo.GitError as e:
        result.errors.append(f"extra branches fetch: {e}")
        return 0
    logger.info(
        "code_ingest: %s mirror %s fetched %d of %d extra branches (%s) in %d ms",
        cfg.label,
        git_dir,
        sent,
        len(gitrepo.match_branches(list(remote), globs)),
        ",".join(globs),
        int((time.monotonic() - now) * 1000),
    )
    return sent


async def fetch_missing_merges(cfg: Any, git_dir: str, repo: Any, result: CodeIngestResult) -> int:
    """One page of the namespace's ledger, the merge commits the mirror lacks
    fetched by sha and pinned. The cursor
    (`merge_sweep_below` in code_index_state) walks pr_number downward one
    page per pass and wraps at the bottom, so every row is checked once a
    cycle. Bounded: one keyed read, one `cat-file --batch-check`, at most
    one fetch of `code_git_ledger_merge_batch` shas. Failures are counted,
    never fatal. Returns the number of shas fetched."""
    from server.code.app.index import gitrepo

    if not settings.code_git_fetch_ledger_merges:
        return 0
    org, ns = cfg.org, cfg.namespace
    page = max(1, int(settings.code_git_ledger_merge_page))
    batch = max(1, int(settings.code_git_ledger_merge_batch))
    try:
        below = int(await repo.get_state(org, ns, "merge_sweep_below") or 0)
        rows = await repo.merge_shas_page(org, ns, below_pr=below, limit=page)
        if not rows:
            if below:
                await repo.set_state(org, ns, "merge_sweep_below", "0", detail="wrapped")
            return 0
        gone = unpinned(git_dir)
        missing = [
            s
            for s in await gitrepo.missing_commits(git_dir, [sha for _, sha in rows])
            if s not in gone
        ]
        fetched = await pin_merges(cfg, git_dir, missing[:batch])
        # a page with more than `batch` missing is re-read next pass (cursor held)
        nxt = rows[-1][0] if len(missing) <= batch else below
        await repo.set_state(
            org,
            ns,
            "merge_sweep_below",
            str(nxt),
            detail=f"checked={len(rows)} missing={len(missing)} fetched={fetched}",
        )
    except gitrepo.GitError as e:
        result.errors.append(f"ledger merges fetch: {e}")
        return 0
    except Exception as e:  # noqa: BLE001 — a ledger read failing must not stop the index
        result.errors.append(f"ledger merges sweep: {type(e).__name__}: {e}")
        return 0
    if fetched:
        logger.info(
            "code_ingest: %s fetched %d ledger merge commits the mirror lacked (%d missing in "
            "a page of %d below #%d)",
            cfg.label,
            fetched,
            len(missing),
            len(rows),
            below or 0,
        )
    return fetched


# git_dir → the ledger merges its remote no longer serves (a fetch by sha answered
# "not our ref" twice, in two calls): never asked for again, counted in /v1/health and
# `loom doctor`. Kept in the mirror (`UNPINNED_FILE`) so a restart and the doctor see them.
_UNPINNED: dict[str, set[str]] = {}
# (git_dir, sha) refused once: skipped for the call, asked for again by the next one.
_REFUSED_ONCE: set[tuple[str, str]] = set()
UNPINNED_FILE = "loom-unpinned-merges"
_GONE = ("not our ref {}", "couldn't find remote ref {}")


def unpinned(git_dir: str) -> set[str]:
    """The merges `git_dir`'s remote refused, read from the mirror once per process."""
    if git_dir not in _UNPINNED:
        try:
            with open(os.path.join(git_dir, UNPINNED_FILE)) as f:
                _UNPINNED[git_dir] = {ln.strip() for ln in f if ln.strip()}
        except OSError:
            _UNPINNED[git_dir] = set()
    return _UNPINNED[git_dir]


def _record_unpinned(git_dir: str, shas: set[str]) -> None:
    gone = unpinned(git_dir)
    gone.update(shas)
    logger.warning("mirror %s: remote no longer serves ledger merges %s", git_dir, sorted(shas))
    try:
        with open(os.path.join(git_dir, UNPINNED_FILE), "w") as f:
            f.write("".join(f"{s}\n" for s in sorted(gone)))
    except OSError as e:
        logger.debug("mirror %s: unpinned list not written: %s", git_dir, e)


async def pin_merges(cfg: Any, git_dir: str, shas: list[str]) -> int:
    """Fetch merge commits by sha and pin them; the number requested. A sha the remote
    refuses ("not our ref": a branch it was on was deleted and gc'd upstream) is left
    out and the rest fetched again; refused in a second call too, it is recorded as
    unpinned. Any other failure raises."""
    from server.code.app.index import gitrepo

    gone = unpinned(git_dir)
    want = [s for s in dict.fromkeys(shas) if s not in gone]
    while want:
        try:
            return await gitrepo.fetch_shas(git_dir, cfg.git_remote, want, token=cfg.github_token)
        except gitrepo.GitError as e:
            named = {s for s in want if any(g.format(s) in str(e) for g in _GONE)}
            if not named:
                raise
            if again := {s for s in named if (git_dir, s) in _REFUSED_ONCE}:
                _record_unpinned(git_dir, again)
            if len(_REFUSED_ONCE) > 4096:
                _REFUSED_ONCE.clear()
            _REFUSED_ONCE.update((git_dir, s) for s in named - again)
            want = [s for s in want if s not in named]
    return 0


# When each shared mirror last fetched PR heads (monotonic s).
_PR_HEADS_FETCHED_AT: dict[str, float] = {}


def _pr_heads_due(git_dir: str, now: float) -> bool:
    gap = max(30.0, float(settings.code_ingest_interval_seconds) * 0.9)
    return now - _PR_HEADS_FETCHED_AT.get(git_dir, -1e12) >= gap


async def fetch_pr_heads(cfg: Any, git_dir: str, repo: Any, result: CodeIngestResult) -> int:
    """The mirror carries `refs/pull/N/head` for the PRs the ledger holds open: one
    `ls-remote` of the PR heads, one `fetch --stdin` of at most `code_git_pr_head_batch`
    refspecs whose upstream tip the clone lacks, newest activity first, no `--prune`; once
    per mirror per pass. A failure is counted and logged, never fatal. Returns the number of
    heads fetched (0 = off, not due, nothing open, or failed)."""
    from server.code.app.index import gitrepo

    if not settings.code_git_fetch_pr_heads or not cfg.git_fetch_pr_heads:
        return 0
    now = time.monotonic()
    if not _pr_heads_due(git_dir, now):
        return 0
    try:
        open_prs = await repo.open_pr_numbers(
            cfg.org, cfg.namespace, limit=max(1000, settings.code_git_pr_head_batch * 20)
        )
    except Exception as e:  # noqa: BLE001 — a lagging ledger read is not the mirror's fault
        result.errors.append(f"pr heads: open PR read failed: {e}")
        return 0
    if not open_prs:
        return 0  # another entry sharing the mirror may hold the ledger; it tries next
    _PR_HEADS_FETCHED_AT[git_dir] = now  # a failing remote is retried next pass, not next entry
    try:
        remote = await gitrepo.remote_pull_heads(git_dir, cfg.git_remote, token=cfg.github_token)
        local = await gitrepo.local_pull_heads(git_dir)
        wanted = gitrepo.pull_heads_to_fetch(
            open_prs, remote, local, limit=settings.code_git_pr_head_batch
        )
        sent = await gitrepo.fetch_pull_heads(
            git_dir, cfg.git_remote, wanted, token=cfg.github_token
        )
    except gitrepo.GitError as e:
        result.errors.append(f"pr heads fetch: {e}")
        return 0
    behind = sum(1 for n in open_prs if remote.get(n) and local.get(n) != remote.get(n))
    logger.info(
        "code_ingest: %s mirror %s fetched %d PR heads (%d of %d open PRs were behind) in %d ms",
        cfg.label,
        git_dir,
        sent,
        behind,
        len(open_prs),
        int((time.monotonic() - now) * 1000),
    )
    try:
        await repo.set_state(
            cfg.org,
            cfg.namespace,
            "pr_heads_fetch",
            str(sent),
            detail=f"open={len(open_prs)} behind={behind} sent={sent}",
        )
    except Exception as e:  # noqa: BLE001 — the state row is a gauge
        logger.debug("code_ingest: pr_heads_fetch state write skipped: %s", e)
    return sent


async def fetch_entry(cfg: Any, git_dir: str, repo: Any, result: CodeIngestResult) -> bool:
    """Everything the pass fetches for one entry, under the mirror's fetch lock: the
    branch (unless `prefetch_mirrors` fetched it), extra branches, ledger merges and
    open PR heads. False when the branch fetch failed."""
    from server.code.app.index import gitrepo

    async with fetch_lock(git_dir):
        try:
            if (git_dir, cfg.git_branch) not in _FETCHED_THIS_PASS:
                await gitrepo.fetch(git_dir, cfg.git_remote, cfg.git_branch, token=cfg.github_token)
        except gitrepo.GitError as e:
            result.errors.append(f"fetch: {e}")
            result.tier_failed(cfg, "fetch", f"fetch {cfg.git_remote}: {e}")
            return False
        result.tier_ok(cfg, "fetch")
        await fetch_extra_branches(cfg, git_dir, result)
        await fetch_missing_merges(cfg, git_dir, repo, result)
        await fetch_pr_heads(cfg, git_dir, repo, result)
        await asyncio.to_thread(mark_fetched, git_dir)
    return True


# The (git_dir, branch) pairs this pass fetched in one batched
# negotiation per mirror; `_run_repo` skips its own fetch for them.
_FETCHED_THIS_PASS: set[tuple[str, str]] = set()


async def prefetch_mirrors(repos: list[Any], result: CodeIngestResult) -> int:
    """One `fetch` per shared mirror for every configured branch it serves.
    A mirror can serve many entries; if each negotiated its own fetch in its
    turn, an entry the budget never reached would never be fetched at all. A
    mirror with one entry fetches in that entry's turn. A failed batch
    leaves its entries to their own fetch. Returns the mirrors fetched."""
    from server.code.app.index import gitrepo

    _FETCHED_THIS_PASS.clear()
    groups: dict[tuple[str, str, str], list[Any]] = {}
    for cfg in repos:
        remote, token = cfg.git_remote, cfg.github_token
        key = (cfg.git_dir, remote, token)
        groups.setdefault(key, []).append(cfg)
    fetched = 0
    for (git_dir, remote, token), cfgs in groups.items():
        if len(cfgs) < 2 or not await asyncio.to_thread(os.path.isdir, git_dir):
            continue
        started = time.monotonic()
        branches = [c.git_branch for c in cfgs]
        try:
            async with fetch_lock(git_dir):
                await gitrepo.fetch_many(git_dir, remote, branches, token=token)
        except gitrepo.GitError as e:
            result.errors.append(f"mirror fetch {git_dir}: {e}")
            continue
        _FETCHED_THIS_PASS.update((git_dir, b) for b in branches)
        fetched += 1
        logger.info(
            "code_ingest: mirror %s fetched %d configured branches in one negotiation in %d ms",
            git_dir,
            len(set(branches)),
            int((time.monotonic() - started) * 1000),
        )
    return fetched
