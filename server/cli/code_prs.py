"""`loom code backfill-prs` and the GitHub history backfills: merged PRs,
reviews, items and assignees."""

from __future__ import annotations

import argparse
import sys

from server.cli._ch import connect_code_plane
from server.cli.code_run import _identity, _note, _repo_cfg
from server.core.datetimes import naive_utc_now


async def _backfill_prs(args: argparse.Namespace) -> int:
    from datetime import date, timedelta

    from server.clients.github import GitHubError, RateLimited, merged_prs_window
    from server.code.store.index import CodeRepo
    from server.code.workers.pr_history import pr_rows
    from server.core.ch import get_client
    from server.settings import settings

    cfg = _repo_cfg(args)
    if not cfg.github_token:
        # Same policy as the code_pr_history worker: public repos read fine
        # anonymously, but at 60 req/h a day of ClickHouse merges (~100 PRs,
        # one files-fetch each) will hit the limit and the run stops on the
        # resumable day cursor. Say so instead of refusing.
        print(
            "no LOOM_CODE_GITHUB_TOKEN — using the anonymous GitHub API (60 req/h); "
            "expect rate-limit stops, re-run to resume",
            file=sys.stderr,
        )
    await connect_code_plane(get_client(), args)
    org, ns = _identity(args)
    repo = CodeRepo()

    months = args.months or settings.code_pr_backfill_months
    oldest = naive_utc_now().date() - timedelta(days=30 * months)
    # merged_at is immutable, so a calendar-day cursor walking BACKWARD is
    # gapless and resumable: pr_backfill_date = the next day to fetch.
    cursor_raw = await repo.get_state(org, ns, "pr_backfill_date")
    day = date.fromisoformat(cursor_raw) if cursor_raw else naive_utc_now().date()

    total = 0
    while day >= oldest and total < args.max_prs:
        try:
            prs = await merged_prs_window(
                cfg.repo, cfg.github_token, day.isoformat(), base=cfg.git_branch
            )
        except RateLimited as e:
            print(f"rate limited ({e}); resume later — cursor kept at {day}", file=sys.stderr)
            return 1
        except GitHubError as e:
            print(f"github error on {day}: {e}", file=sys.stderr)
            return 1
        if prs:
            rows = await pr_rows(repo, org, ns, prs)
            await repo.insert_rows("code_pr_history", rows)
            total += len(rows)
        print(f"  {day}: {len(prs)} merged PRs (total {total})")
        day -= timedelta(days=1)
        await repo.set_state(org, ns, "pr_backfill_date", day.isoformat())
    print(f"backfill-prs: {total} PRs; next resume day {day}")
    _note(prs=total, resume_day=day.isoformat())
    return 0


async def _backfill_reviews(args: argparse.Namespace) -> int:
    from server.clients.github import GitHubError, RateLimited, pr_review_comments
    from server.code.store.index import CodeRepo
    from server.code.store.pr_reviews_rows import review_rows
    from server.core.ch import get_client

    cfg = _repo_cfg(args)
    if not cfg.github_token:
        print(
            "backfill-reviews: LOOM_CODE_GITHUB_TOKEN required (one request per PR)",
            file=sys.stderr,
        )
        return 2
    await connect_code_plane(get_client(), args)
    org, ns = cfg.org, cfg.namespace
    repo = CodeRepo()
    rows = await repo._ch.query_all(
        """
        SELECT h.pr_number AS pr_number
        FROM loom.code_pr_history AS h FINAL
        LEFT ANTI JOIN (
            SELECT DISTINCT pr_number FROM loom.code_pr_reviews
            WHERE org = {org:String} AND namespace = {ns:String}
        ) AS r ON r.pr_number = h.pr_number
        WHERE h.org = {org:String} AND h.namespace = {ns:String} AND h.tombstoned_at IS NULL
        ORDER BY h.merged_at DESC
        LIMIT {n:UInt32}
        """,
        {"org": org, "ns": ns, "n": args.prs},
    )
    todo = [int(r["pr_number"]) for r in rows]
    fetched = written = empty = 0
    for n in todo:
        try:
            comments = await pr_review_comments(cfg.repo, cfg.github_token, n)
        except RateLimited as e:
            print(f"rate limited ({e}); {fetched} PRs done — rerun later", file=sys.stderr)
            break
        except GitHubError as e:
            print(f"#{n}: {e}", file=sys.stderr)
            continue
        fetched += 1
        # The marker row for an empty fetch is what makes this command
        # resumable: without it every comment-less PR came back "pending".
        await repo.insert_rows("code_pr_reviews", review_rows(org, ns, n, comments))
        written += len(comments)
        if not comments:
            empty += 1
    print(
        f"backfill-reviews: {fetched} PRs fetched, {written} comments stored, "
        f"{empty} PRs without inline comments (of {len(todo)} pending)"
    )
    return 0


async def _backfill_items(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta

    from server.clients.github import GitHubError, RateLimited, github_items_since, items_cursor
    from server.code.store.index import CodeRepo, build_row
    from server.core.ch import get_client
    from server.settings import settings

    cfg = _repo_cfg(args)
    if not cfg.github_token:
        print("backfill-items: LOOM_CODE_GITHUB_TOKEN required", file=sys.stderr)
        return 2
    await connect_code_plane(get_client(), args)
    org, ns = cfg.org, cfg.namespace
    repo = CodeRepo()
    if args.since:
        since = datetime.fromisoformat(args.since)
    else:
        cur = await repo.get_state(org, ns, "gh_items_cursor")
        since = (
            datetime.fromisoformat(cur)
            if cur
            else naive_utc_now() - timedelta(days=settings.code_gh_items_backfill_days)
        )
    try:
        items = await github_items_since(cfg.repo, cfg.github_token, since, max_pages=args.pages)
    except RateLimited as e:
        print(f"rate limited ({e}); rerun later — cursor unmoved", file=sys.stderr)
        return 1
    except GitHubError as e:
        print(f"backfill-items: {e}", file=sys.stderr)
        return 1
    if not items:
        print("backfill-items: nothing newer than", since.isoformat())
        return 0
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
        )
        for it in items
    ]
    for i in range(0, len(rows), 1000):
        await repo.insert_rows("code_gh_items", rows[i : i + 1000])
    newest = items_cursor(items) or since
    await repo.set_state(org, ns, "gh_items_cursor", newest.isoformat())
    prs = sum(1 for it in items if it.is_pr)
    print(
        f"backfill-items: {len(items)} items ({prs} PRs, {len(items) - prs} issues) since "
        f"{since.date()} → cursor {newest.isoformat()}; walked up to {args.pages} pages"
    )
    return 0


async def _backfill_assignees(args: argparse.Namespace) -> int:
    from server.clients.github import GitHubError, RateLimited, merged_pr_assignees
    from server.code.store.index import CODE_PR_COLS, CodeRepo, build_row
    from server.core.ch import get_client

    cfg = _repo_cfg(args)
    if not cfg.github_token:
        print("backfill-assignees: LOOM_CODE_GITHUB_TOKEN required", file=sys.stderr)
        return 2
    await connect_code_plane(get_client(), args)
    org, ns = cfg.org, cfg.namespace
    repo = CodeRepo()
    try:
        fetched = await merged_pr_assignees(cfg.repo, cfg.github_token, pages=args.pages)
    except RateLimited as e:
        print(f"rate limited ({e}); rerun later", file=sys.stderr)
        return 1
    except GitHubError as e:
        print(f"backfill-assignees: {e}", file=sys.stderr)
        return 1
    with_assignees = {n: a for n, a in fetched.items() if a}
    if not with_assignees:
        print("backfill-assignees: nothing to write")
        return 0
    # Re-emit the stored row with assignees set (ReplacingMergeTree: every
    # column travels, a fresh version wins); rows already carrying the same
    # assignees are left alone so the command is idempotent.
    rows = await repo._ch.query_all(
        """
        SELECT * FROM loom.code_pr_history FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND pr_number IN {nums:Array(UInt32)} AND tombstoned_at IS NULL
        """,
        {"org": org, "ns": ns, "nums": list(with_assignees)},
    )
    out = []
    for r in rows:
        want = with_assignees[int(r["pr_number"])]
        if [str(a) for a in (r.get("assignees") or [])] == want:
            continue
        clean = {}
        for k, v in r.items():
            if k not in CODE_PR_COLS or k == "version":
                continue
            clean[k] = v.decode("utf-8", "ignore").rstrip("\x00") if isinstance(v, bytes) else v
        clean["assignees"] = want
        out.append(build_row("code_pr_history", **clean))
    for i in range(0, len(out), 500):
        await repo.insert_rows("code_pr_history", out[i : i + 500])
    print(
        f"backfill-assignees: {len(fetched)} merged PRs listed, {len(with_assignees)} with "
        f"assignees, {len(rows)} stored here, {len(out)} rows updated"
    )
    return 0
