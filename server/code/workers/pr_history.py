"""Merged-PR history sync from the GitHub REST API (cursor-resumable).

Keeps `loom.code_pr_history` current so code.history / code.examples /
context packs can rank by ownership and recency. The cursor is the max
`updated_at` seen (monotone under GitHub's sort=updated&direction=asc);
re-seeing the tail is absorbed by deterministic RMT upserts. A rate-limit
or 5xx leaves the cursor unmoved — pass over, never a gap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.core.datetimes import naive_utc_now
from server.settings import settings
from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass

logger = logging.getLogger("loom.workers.code_pr_history")

_warned_no_token = False
_BUDGET_FRACTION = 0.75


@dataclass
class CodePrHistoryResult:
    heads_filled: int = 0
    prs_upserted: int = 0
    reviews_upserted: int = 0
    cursor: str = ""
    errors: list[str] = field(default_factory=list)
    note: str = ""


async def run_pass() -> CodePrHistoryResult:
    result = CodePrHistoryResult()
    with record_pass("code_pr_history") as rec:
        await _run(result)
        log_pass_errors(logger, "code_pr_history", result.errors)
        rec.outcome = {
            "prs_upserted": result.prs_upserted,
            "reviews_upserted": result.reviews_upserted,
            "heads_filled": result.heads_filled,
            "cursor": result.cursor,
            "errors": len(result.errors),
            "note": result.note,
        }
    return result


async def _run(result: CodePrHistoryResult) -> None:
    from server.code.app.index.bootstrap import awaits_first_index
    from server.code.app.index.pass_budget import rotate_to
    from server.code.app.index.repo_registry import registry_read
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo

    repo = CodeRepo()
    budget = _pass_budget()
    # The pass starts at the entry the previous one did not finish, so the
    # entries after a cut are served before the ones already served: a slow
    # entry early in the list cannot keep the later ones from ever syncing.
    if not registry_read():
        # Unread, an operator entry for a namespace a member added is not skipped yet,
        # and its PR history would be written into that member's namespace.
        result.note = "the self-service registry is unread; nothing synced"
        return
    configured = configured_repos()
    repos = rotate_to(configured, pass_context.resume_cursor())
    for i, cfg in enumerate(repos):
        if budget.exhausted():
            pass_context.set_cursor(cfg.namespace)
            result.note = f"budget reached; the next pass starts at {cfg.namespace}"
            logger.warning(
                "code_pr_history: pass budget (%.0f s) reached before %d entr%s: %s",
                budget.seconds,
                len(repos) - i,
                "y" if len(repos) - i == 1 else "ies",
                ",".join(c.namespace for c in repos[i:]),
            )
            return
        # A pass cancelled inside this entry resumes at the next one, so one
        # entry that outlasts the deadline does not stall the rest; a pass that
        # finishes leaves the configured order's first entry.
        nxt = repos[i + 1] if i + 1 < len(repos) else configured[0]
        pass_context.set_cursor(nxt.namespace)
        pass_context.set_namespace(cfg.namespace)
        if cfg.provider != "github":
            result.note = f"PR history is not available for {cfg.provider} remotes"
            continue
        try:
            if await awaits_first_index(cfg, repo):
                result.note = f"{cfg.label}: no git_head watermark yet; run `loom code backfill`"
                continue
            await _sync_repo(cfg, repo, result)
            await _sync_items(cfg, repo, result)
            await _fill_pr_heads(cfg, repo, result)
        except Exception as e:
            result.errors.append(f"{cfg.label}: {type(e).__name__}: {e}")
            logger.exception("code_pr_history: repo %s failed", cfg.label)
            pass_context.note_failure(e, cfg.namespace)


def _pass_budget() -> Any:
    """Stop starting entries at three quarters of the supervisor's deadline, so a
    pass ends cleanly with its cursor instead of being cancelled mid-entry."""
    from server.code.app.index.pass_budget import PassBudget
    from server.workers.registry import default_max_pass_seconds

    started = time.monotonic()
    deadline = default_max_pass_seconds(settings.code_pr_history_interval_seconds)
    return PassBudget(started, started + deadline * _BUDGET_FRACTION)


async def pr_rows(repo: Any, org: str, ns: str, prs: list[Any]) -> list[dict[str, Any]]:
    """Full code_pr_history rows for fetched PRs, lexemes included: title +
    description with the template stripped + the titles of
    the linked issues the items sync already holds. The title lookup is one
    read; a missing title leaves the numbers in linked_issues regardless.
    The numbers are parsed out of the full description, not the capped excerpt."""
    from server.code.domain.pr_lexemes import linked_issues
    from server.code.store.index.rows import pr_history_row

    linked = {pr.number: linked_issues(pr.body) for pr in prs}
    wanted = sorted({n for numbers in linked.values() for n in numbers})
    titles: dict[int, str] = {}
    if wanted:
        try:
            titles = await repo.gh_item_titles(org, ns, wanted)
        except Exception as e:  # noqa: BLE001 — titles enrich, never gate
            logger.debug("code_pr_history: linked-issue titles skipped: %s", e)
    return [
        pr_history_row(
            org=org,
            namespace=ns,
            pr_number=pr.number,
            title=pr.title,
            body_excerpt=pr.body_excerpt,
            author=pr.author,
            labels=list(pr.labels),
            files=list(pr.files),
            assignees=list(pr.assignees),
            additions=pr.additions,
            deletions=pr.deletions,
            merge_commit_sha=pr.merge_commit_sha,
            merged_at=pr.merged_at,
            opened_at=pr.opened_at,
            linked=linked[pr.number],
            issue_titles=[titles[n] for n in linked[pr.number] if titles.get(n)],
            target_branch=str(getattr(pr, "target_branch", "") or ""),
        )
        for pr in prs
    ]


async def _sync_repo(cfg: Any, repo: Any, result: CodePrHistoryResult) -> None:
    global _warned_no_token
    from server.clients.github import GitHubError, RateLimited, merged_prs_since

    token = cfg.github_token
    if not token:
        # Public repos read fine anonymously (60 req/h shared per IP; one
        # pass is ~1 request). A private repo 404s and lands in errors —
        # the operator sees it on the Health tab instead of a silent no-op.
        if not _warned_no_token:
            logger.warning(
                "code_pr_history: no github token for %s — anonymous GitHub API "
                "(60 req/h); set LOOM_CODE_GITHUB_TOKEN for headroom",
                cfg.label,
            )
            _warned_no_token = True

    org, ns = cfg.org, cfg.namespace

    cursor_raw = await repo.get_state(org, ns, "pr_cursor")
    since: datetime | None
    if cursor_raw:
        since = datetime.fromisoformat(cursor_raw)
    else:
        since = naive_utc_now() - timedelta(days=30 * settings.code_pr_backfill_months)

    try:
        # Only PRs merged INTO this entry's branch: a release-branch namespace
        # is about its backports, not master's firehose.
        prs = await merged_prs_since(cfg.repo, token, since, max_prs=40, base=cfg.git_branch)
    except RateLimited as e:
        result.note = f"rate limited: {e}"
        return
    except GitHubError as e:
        # Logged as well as counted: a private repo 404ing or a revoked token
        # would otherwise show only as a count on the Health tab.
        logger.warning("code_pr_history: %s github error (cursor kept): %s", cfg.label, e)
        result.errors.append(f"{cfg.label}: {e}")
        return

    if not prs:
        result.cursor = cursor_raw
        return

    rows = await pr_rows(repo, org, ns, prs)
    pass_context.check_stop()  # after the GitHub reads: a lost lease writes nothing
    await repo.insert_rows("code_pr_history", rows)
    result.prs_upserted = len(rows)
    if token and settings.code_pr_reviews_enabled:
        result.reviews_upserted += await _sync_reviews(
            cfg, repo, org, ns, [pr.number for pr in prs]
        )
    new_cursor = max(pr.updated_at for pr in prs).isoformat()
    await repo.set_state(org, ns, "pr_cursor", new_cursor)
    result.cursor = new_cursor


async def _sync_reviews(cfg: Any, repo: Any, org: str, ns: str, pr_numbers: list[int]) -> int:
    """Inline review comments for these PRs → loom.code_pr_reviews. One
    request per PR; a rate limit stops the batch (the PR rows are already
    written, the backfill CLI catches up). Errors never fail the pass."""
    from server.clients.github import GitHubError, RateLimited, pr_review_comments
    from server.code.store.pr_reviews_rows import review_rows

    written = 0
    rows: list[dict[str, Any]] = []
    for n in pr_numbers:
        try:
            comments = await pr_review_comments(cfg.repo, cfg.github_token, n)
        except RateLimited:
            logger.warning("code_pr_history: rate limited fetching reviews for #%d; stopping", n)
            break
        except GitHubError as e:
            logger.debug("code_pr_history: reviews for #%d: %s", n, e)
            continue
        # An empty fetch still writes its marker row (comment_id 0), so the
        # backfill never re-fetches a comment-less PR.
        rows.extend(review_rows(org, ns, n, comments))
        written += len(comments)
    # One insert for the batch's PRs (the table is ReplacingMergeTree, so a
    # retried pass rewrites the same rows).
    if rows:
        pass_context.check_stop()
        await repo.insert_rows("code_pr_reviews", rows)
    return written


async def _sync_items(cfg: Any, repo: Any, result: CodePrHistoryResult) -> None:
    """Issues + PRs in any state updated since the cursor → loom.code_gh_items.
    Cursor = max GitHub updated_at seen; a GitHub error
    leaves it unmoved. Skipped without a token (the 60 req/h anonymous
    budget belongs to the PR list)."""
    if not settings.code_gh_items_enabled or not cfg.github_token:
        return
    from server.clients.github import GitHubError, RateLimited, github_items_since, items_cursor
    from server.code.store.index import build_row

    org, ns = cfg.org, cfg.namespace
    cursor_raw = await repo.get_state(org, ns, "gh_items_cursor")
    since = (
        datetime.fromisoformat(cursor_raw)
        if cursor_raw
        else naive_utc_now() - timedelta(days=settings.code_gh_items_backfill_days)
    )
    try:
        items = await github_items_since(cfg.repo, cfg.github_token, since, max_pages=5)
    except RateLimited as e:
        result.note = f"rate limited (items): {e}"
        return
    except GitHubError as e:
        logger.warning("code_gh_items: %s github error (cursor kept): %s", cfg.label, e)
        result.errors.append(f"{cfg.label} items: {e}")
        return
    if not items:
        return
    # The upsert replaces the whole row, so the head / base /
    # files a previous pass filled ride along instead of being blanked; a
    # pushed head is refilled by _fill_pr_heads when the mirror's tip moves.
    heads = await _carried_heads(repo, org, ns, [it.number for it in items if it.is_pr])
    rows = [
        build_row(
            "code_gh_items",
            org=org,
            namespace=ns,
            number=it.number,
            is_pr=it.is_pr,
            state=it.state,
            state_reason=it.state_reason,
            author=it.author,
            title=it.title,
            labels=list(it.labels),
            opened_at=it.opened_at,
            closed_at=it.closed_at,
            merged_at=it.merged_at,
            updated_at=it.updated_at,
            **_head_cols(heads.get(it.number)),
        )
        for it in items
    ]
    pass_context.check_stop()
    await repo.insert_rows("code_gh_items", rows)
    nxt = items_cursor(items)
    if nxt is not None:
        await repo.set_state(org, ns, "gh_items_cursor", nxt.isoformat())


# ---- an open PR's head, base and files -----------------------------------------


def _isdir(path: str) -> bool:
    import os

    return os.path.isdir(path)


def _head_cols(prev: dict[str, Any] | None) -> dict[str, Any]:
    if not prev:
        return {}
    return {
        "head_sha": str(prev.get("head_sha") or ""),
        "base_sha": str(prev.get("base_sha") or ""),
        "files": [str(f) for f in (prev.get("files") or [])],
    }


async def _carried_heads(
    repo: Any, org: str, ns: str, numbers: list[int]
) -> dict[int, dict[str, Any]]:
    if not numbers:
        return {}
    heads: dict[int, dict[str, Any]] = await repo.pr_heads_by_number(org, ns, numbers)
    return heads


def _row_from_item(org: str, ns: str, it: dict[str, Any], **head: Any) -> dict[str, Any]:
    from server.code.store.index import build_row

    return build_row(
        "code_gh_items",
        org=org,
        namespace=ns,
        number=int(it["number"]),
        is_pr=True,
        state=str(it.get("state") or "open"),
        state_reason=str(it.get("state_reason") or ""),
        author=str(it.get("author") or ""),
        title=str(it.get("title") or ""),
        labels=[str(x) for x in (it.get("labels") or [])],
        opened_at=it.get("opened_at"),
        closed_at=it.get("closed_at"),
        merged_at=it.get("merged_at"),
        updated_at=it.get("updated_at"),
        **head,
    )


async def _fill_from_mirror(
    cfg: Any,
    git_dir: str,
    rows: list[dict[str, Any]],
    local: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rewritten rows, rows the mirror cannot serve). A row is rewritten
    only when the mirror's tip differs from its `head_sha`: base = the
    merge-base with the entry's branch, files = the range's paths."""
    from server.code.app.index import gitrepo

    out: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    branch_ref = f"refs/heads/{cfg.git_branch}"
    for it in rows:
        n = int(it["number"])
        tip = local.get(n, "")
        if not tip:
            missing.append(it)
            continue
        if str(it.get("head_sha") or "") == tip and it.get("files"):
            continue  # filled from this very head already
        base = await gitrepo.merge_base(git_dir, tip, branch_ref)
        files = await gitrepo.diff_name_only(git_dir, base, tip) if base else []
        out.append(
            _row_from_item(cfg.org, cfg.namespace, it, head_sha=tip, base_sha=base, files=files)
        )
    return out, missing


async def _fill_from_github(
    cfg: Any, rows: list[dict[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    """One `GET /pulls/N` per row the mirror lacks (head and base only; files
    follow once the ingest fetches the head), at most `limit`, paced; a rate
    limit stops the batch."""
    import asyncio

    from server.clients.github import (
        ITEMS_PAGE_PACE_S,
        GitHubError,
        RateLimited,
        pr_head_and_base,
    )

    out: list[dict[str, Any]] = []
    for it in [r for r in rows if not str(r.get("head_sha") or "")][: max(0, limit)]:
        try:
            head, base = await pr_head_and_base(cfg.repo, cfg.github_token, int(it["number"]))
        except RateLimited:
            logger.warning(
                "code_gh_items: rate limited fetching PR head #%s; stopping", it["number"]
            )
            break
        except GitHubError as e:
            logger.debug("code_gh_items: PR head #%s: %s", it["number"], e)
            continue
        if head:
            out.append(
                _row_from_item(cfg.org, cfg.namespace, it, head_sha=head, base_sha=base, files=[])
            )
        await asyncio.sleep(ITEMS_PAGE_PACE_S)
    return out


async def _fill_pr_heads(cfg: Any, repo: Any, result: CodePrHistoryResult) -> None:
    """head_sha / base_sha / files on open-PR rows: from the mirror's
    `refs/pull/N/head` first — no GitHub call — then one `GET /pulls/N` per
    row the mirror lacks, bounded and paced. New ReplacingMergeTree versions,
    never ALTER UPDATE; errors never fail the pass."""
    import asyncio

    from server.code.app.index import gitrepo

    if not settings.code_gh_items_enabled:
        return
    org, ns = cfg.org, cfg.namespace
    try:
        rows = await repo.open_prs_for_head_fill(
            org, ns, limit=settings.code_gh_items_head_fill_batch
        )
    except Exception as e:  # noqa: BLE001 — errors never fail the pass
        logger.debug("code_gh_items: head fill skipped (%s)", e)
        return
    if not rows:
        return
    git_dir = cfg.git_dir
    local: dict[int, str] = {}
    has_mirror: bool = bool(git_dir) and await asyncio.to_thread(_isdir, git_dir)
    if has_mirror:
        try:
            local = await gitrepo.local_pull_heads(git_dir)
        except gitrepo.GitError as e:
            result.errors.append(f"{cfg.label} pr heads: {e}")
    filled, missing = await _fill_from_mirror(cfg, git_dir, rows, local) if local else ([], rows)
    if cfg.github_token and missing:
        filled.extend(
            await _fill_from_github(cfg, missing, limit=settings.code_gh_items_head_github_batch)
        )
    if filled:
        pass_context.check_stop()
        await repo.insert_rows("code_gh_items", filled)
        result.heads_filled += len(filled)
        logger.info(
            "code_gh_items: %s filled head/base/files on %d open PRs (%d from the mirror, %d open "
            "rows read, %d without a mirror head)",
            cfg.label,
            len(filled),
            sum(1 for r in filled if r.get("files")),
            len(rows),
            len(missing),
        )
