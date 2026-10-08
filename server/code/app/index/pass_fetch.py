"""What the ingest pass fetches into a namespace's mirror before it walks: the branch,
extra release branches, merges the ledger names and open PR heads."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from server.code.app.index.pass_budget import CodeIngestResult
from server.settings import settings

logger = logging.getLogger("loom.workers.code_ingest")


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
        missing = await gitrepo.missing_commits(git_dir, [sha for _, sha in rows])
        fetched = await gitrepo.fetch_shas(
            git_dir, cfg.git_remote, missing[:batch], token=cfg.github_token
        )
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


# The (git_dir, branch) pairs this pass fetched in one batched
# negotiation per mirror; `_run_repo` skips its own fetch for them.
_FETCHED_THIS_PASS: set[tuple[str, str]] = set()


async def prefetch_mirrors(repos: list[Any], result: CodeIngestResult) -> int:
    """One `fetch` per shared mirror for every configured branch it serves.
    A mirror can serve many entries; if each negotiated its own fetch in its
    turn, an entry the budget never reached would never be fetched at all. A
    mirror with one entry fetches in that entry's turn. A failed batch
    leaves its entries to their own fetch. Returns the mirrors fetched."""
    import os

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
