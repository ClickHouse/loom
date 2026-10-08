"""Minimal GitHub REST client for the code plane.

Reads merged PRs with their files and assignees (`loom.code_pr_history`),
issues and PRs in any state (`loom.code_gh_items`), inline review comments
(`loom.code_pr_reviews`), one PR's head/base or target branch, and whether a
repository is a fork. Uses the shared httpx pool; the token is the caller's.
A rate limit raises ``RateLimited`` and any other failed read (a timeout or
an unreachable API included) ``GitHubError``;
callers stop or skip with their cursor unmoved — state never advances on a
failed fetch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from server.clients._http import get_client
from server.core.datetimes import naive_utc_now

logger = logging.getLogger("loom.clients.github")

_API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class RateLimited(GitHubError):
    pass


@dataclass(frozen=True)
class MergedPR:
    number: int
    title: str
    body_excerpt: str
    author: str
    labels: list[str]
    merge_commit_sha: str
    merged_at: datetime
    updated_at: datetime
    opened_at: datetime | None = None
    additions: int = 0
    deletions: int = 0
    files: list[str] = field(default_factory=list)
    # Who the repository put on the PR to review it: GitHub assignees plus
    # requested reviewers, author removed. Where a bot assigns the reviewer,
    # who often leaves no inline comment, this is the primary "who reviewed"
    # signal.
    assignees: list[str] = field(default_factory=list)
    # The FULL description: the stored excerpt is capped, and the issues a
    # PR closes are read from all of it.
    body: str = ""
    # The branch the PR merged into (GitHub's `base.ref`):
    # `master`, a release branch (`26.3`), the backport bot's staging branch
    # (`backport/26.3/118740`) or a feature branch. '' = not read.
    target_branch: str = ""


def _headers(token: str) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "loom-code-intel/1.0 (+https://github.com/ClickHouse/loom)",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _rate_limited(resp: Any) -> bool:
    """GitHub answers both an exhausted primary budget and a secondary limit
    with 403 or 429: the primary with `X-RateLimit-Remaining: 0`, the
    secondary with a `Retry-After` header or a message naming the limit. A
    403 with none of these is a permission refusal."""
    if resp.status_code == 429:
        return True
    if resp.status_code != 403:
        return False
    if resp.headers.get("X-RateLimit-Remaining") == "0" or resp.headers.get("Retry-After"):
        return True
    return "rate limit" in resp.text[:500].lower()


async def _get(url: str, token: str, params: dict[str, Any] | None = None) -> Any:
    """One GET's JSON body. Every failure is a `GitHubError` (a rate limit its
    `RateLimited` subclass): a timeout, a refused connection or a non-JSON
    body included, so a caller's "stop or skip, cursor unmoved" handling
    covers them too."""
    try:
        resp = await get_client().get(url, headers=_headers(token), params=params)
    except httpx.RequestError as e:
        raise GitHubError(f"GET {url} -> {type(e).__name__}: {e}") from e
    if _rate_limited(resp):
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            raise RateLimited(f"rate limited ({resp.status_code}); retry after {retry_after}s")
        raise RateLimited(f"rate limited until {resp.headers.get('X-RateLimit-Reset')}")
    if resp.status_code >= 400:
        raise GitHubError(f"GET {url} -> {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise GitHubError(f"GET {url} -> {resp.status_code}: body is not JSON") from e


async def _get_object(url: str, token: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """An object endpoint's body; any other JSON shape is a `GitHubError`."""
    body = await _get(url, token, params)
    if not isinstance(body, dict):
        raise GitHubError(f"GET {url} -> expected a JSON object, got {type(body).__name__}")
    return body


async def _get_list(
    url: str, token: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """A list endpoint's items; a body that is not a JSON array is a
    `GitHubError`, and an item that is not an object is dropped, so a walk
    never fails on `.get` of something GitHub did not promise."""
    body = await _get(url, token, params)
    if not isinstance(body, list):
        raise GitHubError(f"GET {url} -> expected a JSON array, got {type(body).__name__}")
    return [item for item in body if isinstance(item, dict)]


async def repo_fork_upstreams(repo: str, token: str) -> list[str]:
    """`[parent.full_name, source.full_name]` (de-duplicated,
    in that order) when `repo` (owner/name) is a fork; `[]` when it is not.
    Raises `GitHubError` on a failed read (404 included: a private fork the
    token cannot see)."""
    d = await _get_object(f"{_API}/repos/{repo}", token)
    if d.get("fork") is not True:
        return []
    out: list[str] = []
    for key in ("parent", "source"):
        name = (d.get(key) or {}).get("full_name") if isinstance(d.get(key), dict) else None
        if isinstance(name, str) and name and name not in out:
            out.append(name)
    return out


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


async def merged_prs_since(
    repo: str,
    token: str,
    since: datetime | None,
    *,
    max_prs: int = 50,
    base: str = "",
) -> list[MergedPR]:
    """Merged PRs updated after `since`, with files, oldest-updated first.

    `base` restricts to PRs merged INTO that branch (GitHub's own `base`
    filter). A release-branch namespace wants its backports, not master's
    firehose; master wants master's.

    Pages `/pulls?sort=updated&direction=desc` (GitHub cannot seek ascending
    to a timestamp — ascending starts at the repo's first PR) and stops as
    soon as a page dips below `since`. Correct for the INCREMENTAL cadence,
    where the burst since the cursor is at most a few pages; deep history
    goes through `merged_prs_window` (search API, immutable date windows).
    """
    collected: list[MergedPR] = []
    page = 1
    while len(collected) < max_prs and page <= 10:
        prs = await _get_list(
            f"{_API}/repos/{repo}/pulls",
            token,
            {
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 50,
                "page": page,
                **({"base": base} if base else {}),
            },
        )
        if not prs:
            break
        drained = False
        for pr in prs:
            updated = _parse_dt(pr.get("updated_at"))
            if since is not None and updated is not None and updated <= since:
                drained = True
                break
            merged = _parse_dt(pr.get("merged_at"))
            if merged is None:
                continue  # closed without merging
            collected.append(await _hydrate_pr(repo, token, pr, merged, updated))
            if len(collected) >= max_prs:
                break
        if drained:
            break
        page += 1
    collected.sort(key=lambda p: p.updated_at)
    return collected


async def merged_prs_window(
    repo: str,
    token: str,
    day: str,
    *,
    max_prs: int = 1000,
    base: str = "",
) -> list[MergedPR]:
    """All PRs MERGED on one calendar day (UTC), via the search API.

    merged_at is immutable, so day windows are a gapless, resumable
    backfill cursor (walk backward one day at a time). GitHub search caps
    a query at 1000 results; ClickHouse merges ~50-150 PRs/day, far under.
    Search rate limit is 30 req/min — the caller paces day-by-day anyway.
    """
    out: list[MergedPR] = []
    page = 1
    while len(out) < max_prs and page <= 10:
        res = await _get_object(
            f"{_API}/search/issues",
            token,
            {
                "q": (
                    f"repo:{repo} is:pr is:merged merged:{day}..{day}"
                    + (f" base:{base}" if base else "")
                ),
                "per_page": 100,
                "page": page,
            },
        )
        items = res.get("items") or []
        if not isinstance(items, list) or not items:
            break
        for item in items:
            number = int(item.get("number") or 0) if isinstance(item, dict) else 0
            if not number:
                continue
            pr = await _get_object(f"{_API}/repos/{repo}/pulls/{number}", token)
            merged = _parse_dt(pr.get("merged_at"))
            if merged is None:
                continue
            updated = _parse_dt(pr.get("updated_at"))
            out.append(await _hydrate_pr(repo, token, pr, merged, updated))
        if len(items) < 100:
            break
        page += 1
    return out


async def _hydrate_pr(
    repo: str,
    token: str,
    pr: dict[str, Any],
    merged: datetime,
    updated: datetime | None,
) -> MergedPR:
    files = await pr_files(repo, token, int(pr["number"]))
    return MergedPR(
        number=int(pr["number"]),
        title=str(pr.get("title") or "")[:500],
        body_excerpt=str(pr.get("body") or "")[:2000],
        author=str((pr.get("user") or {}).get("login") or ""),
        labels=[str(lb.get("name") or "") for lb in pr.get("labels") or []][:20],
        merge_commit_sha=str(pr.get("merge_commit_sha") or ""),
        merged_at=merged,
        updated_at=updated or merged,
        opened_at=_parse_dt(pr.get("created_at")),
        additions=int(pr.get("additions") or 0),
        deletions=int(pr.get("deletions") or 0),
        files=files,
        assignees=pr_assignees(pr),
        body=str(pr.get("body") or ""),
        target_branch=pr_base_ref(pr),
    )


def pr_base_ref(pr: dict[str, Any]) -> str:
    """`base.ref` of a REST pull-request object, '' when absent."""
    base = pr.get("base") or {}
    return str(base.get("ref") or "")[:200] if isinstance(base, dict) else ""


async def pr_head_and_base(repo: str, token: str, number: int) -> tuple[str, str]:
    """One PR's `head.sha` and `base.sha` (`GET /pulls/{number}`; the
    fallback for an open PR whose head the mirror has not fetched).
    GitHub's `base.sha` is the base branch's tip at the PR's last update, not
    the merge-base — the mirror path replaces it once the head is fetched."""
    pr = await _get_object(f"{_API}/repos/{repo}/pulls/{int(number)}", token)
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    return (
        str(head.get("sha") or "")[:40] if isinstance(head, dict) else "",
        str(base.get("sha") or "")[:40] if isinstance(base, dict) else "",
    )


def pr_assignees(pr: dict[str, Any]) -> list[str]:
    """Assignees ∪ requested reviewers of a PR object, author excluded,
    first-seen order, at most 10 (search-API items carry the same keys)."""
    author = str((pr.get("user") or {}).get("login") or "").lower()
    out: list[str] = []
    for key in ("assignees", "requested_reviewers"):
        for u in pr.get(key) or []:
            login = str((u or {}).get("login") or "").strip()
            if login and login.lower() != author and login.lower() not in {o.lower() for o in out}:
                out.append(login)
    return out[:10]


@dataclass(frozen=True)
class GhItem:
    """One GitHub issue or pull request in any state — the source of record
    for "does #N exist, is it a PR or an issue, who opened it, what state was
    it in at time T". code_pr_history holds only MERGED PRs, and agents cite
    open PRs and issues far more often."""

    number: int
    is_pr: bool
    state: str  # open | closed
    state_reason: str  # completed | not_planned | reopened | ""
    author: str
    title: str
    labels: list[str]
    opened_at: datetime
    closed_at: datetime | None
    merged_at: datetime | None
    updated_at: datetime


def parse_gh_item(d: dict[str, Any]) -> GhItem | None:
    number = int(d.get("number") or 0)
    opened = _parse_dt(d.get("created_at"))
    updated = _parse_dt(d.get("updated_at")) or opened
    if not number or opened is None or updated is None:
        return None
    pr = d.get("pull_request") or None
    return GhItem(
        number=number,
        is_pr=pr is not None,
        state=str(d.get("state") or "")[:16],
        state_reason=str(d.get("state_reason") or "")[:32],
        author=str((d.get("user") or {}).get("login") or "")[:100],
        title=str(d.get("title") or "")[:500],
        labels=[str(lb.get("name") or "") for lb in d.get("labels") or []][:20],
        opened_at=opened,
        closed_at=_parse_dt(d.get("closed_at")),
        merged_at=_parse_dt((pr or {}).get("merged_at")) if pr else None,
        updated_at=updated,
    )


# Seconds between list pages. GitHub's secondary rate limit trips on rapid
# pagination of a list endpoint well before the 5,000/h primary budget (an
# unpaced 100-page walk trips it around page 50); ~1 request/s is inside it.
ITEMS_PAGE_PACE_S = 0.7


def items_cursor(items: list[GhItem], per_page: int = 100) -> datetime | None:
    """The `since` to resume an ascending updated-order walk from.

    GitHub sorts by an internal timestamp that is NOT always the `updated_at` it
    returns: on a busy repository about one item per 100-item page carries an
    `updated_at` months ahead of its sort position, so `max(updated_at)` as the cursor
    skips whole windows outright. Use a positional cursor instead: when the walk
    returned at least `per_page` items, the 20th percentile of the `updated_at` of the
    LAST `per_page` items (outliers are rare and high, so this sits inside their true
    window; the overlap is re-fetched and de-duplicated by the ReplacingMergeTree);
    when the whole walk returned fewer than `per_page` items it was one short page that
    reached the present, and max is safe. The test is the walk's total, not its last
    page: a multi-page walk ending on a short page still takes the percentile, over a
    tail that spans that page and the end of the one before it."""
    if not items:
        return None
    tail = items[-per_page:] if len(items) >= per_page else items
    if len(items) < per_page:
        return max(i.updated_at for i in tail)
    ts = sorted(i.updated_at for i in tail)
    return ts[len(ts) // 5]


async def github_items_since(
    repo: str,
    token: str,
    since: datetime | None,
    *,
    max_pages: int = 5,
    per_page: int = 100,
    pace_s: float = ITEMS_PAGE_PACE_S,
) -> list[GhItem]:
    """Issues AND pull requests (any state) updated after `since`, oldest
    update first, `max_pages` × `per_page` at most — the `/issues` list
    endpoint returns both kinds with state, author, dates and a
    `pull_request` marker, so one request covers 100 items. GitHub honours
    `since` with ascending order; callers resume from `items_cursor(items)`,
    not the max `updated_at` seen (see there for why).

    Progress is kept: a RateLimited (or any GitHubError) on page N > 1
    returns the pages already fetched so the caller can persist them and
    advance the cursor; only a failure on the FIRST page raises. Pages are
    paced by `pace_s` to stay under the secondary rate limit."""
    out: list[GhItem] = []
    for page in range(1, max_pages + 1):
        params: dict[str, Any] = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": per_page,
            "page": page,
        }
        if since is not None:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            items = await _get_list(f"{_API}/repos/{repo}/issues", token, params)
        except GitHubError as e:
            if not out:
                raise
            logger.info(
                "github items: stopping at page %d after %d items (%s); progress kept",
                page,
                len(out),
                e,
            )
            break
        if not items:
            break
        for d in items:
            it = parse_gh_item(d)
            if it is not None:
                out.append(it)
        if len(items) < per_page:
            break
        if pace_s > 0 and page < max_pages:
            await asyncio.sleep(pace_s)
    return out


@dataclass
class ReviewComment:
    comment_id: int
    path: str
    line: int
    author: str
    is_bot: bool
    body: str
    in_reply_to: int
    created_at: datetime


def parse_review_comment(d: dict[str, Any]) -> ReviewComment | None:
    """One `/pulls/{n}/comments` item → ReviewComment; None when it has no
    id or no body. Bots are flagged, not dropped (GitHub marks them with
    user.type == "Bot" or a `[bot]` login suffix). Bodies cut at 2,000."""
    try:
        cid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        return None
    body = str(d.get("body") or "").strip()
    if not cid or not body:
        return None
    user = d.get("user") or {}
    login = str(user.get("login") or "")
    is_bot = str(user.get("type") or "") == "Bot" or login.endswith("[bot]")
    line = d.get("line") if d.get("line") is not None else d.get("original_line")
    created = _parse_dt(d.get("created_at")) or naive_utc_now()
    return ReviewComment(
        comment_id=cid,
        path=str(d.get("path") or "")[:1000],
        line=int(line or 0),
        author=login[:200],
        is_bot=is_bot,
        body=body[:2000],
        in_reply_to=int(d.get("in_reply_to_id") or 0),
        created_at=created,
    )


async def pr_review_comments(
    repo: str, token: str, number: int, *, max_pages: int = 3
) -> list[ReviewComment]:
    """Inline review comments of one PR (diff-anchored; capped at
    max_pages*100). Review summaries ("LGTM") live on /reviews and are not
    fetched — they carry no path and rarely a convention."""
    out: list[ReviewComment] = []
    for page in range(1, max_pages + 1):
        rows = await _get_list(
            f"{_API}/repos/{repo}/pulls/{number}/comments",
            token,
            {"per_page": 100, "page": page},
        )
        if not rows:
            break
        for r in rows:
            rc = parse_review_comment(r)
            if rc is not None:
                out.append(rc)
        if len(rows) < 100:
            break
    return out


async def merged_pr_assignees(
    repo: str,
    token: str,
    *,
    pages: int = 20,
    per_page: int = 100,
    base: str = "",
) -> dict[int, list[str]]:
    """pr_number -> assignees for the newest `pages` × `per_page` closed PRs
    (merged only). One request per page, no per-PR hydration: the list
    endpoint already carries assignees and requested_reviewers. Feeds
    `loom code backfill-assignees`."""
    out: dict[int, list[str]] = {}
    for page in range(1, pages + 1):
        prs = await _get_list(
            f"{_API}/repos/{repo}/pulls",
            token,
            {
                "state": "closed",
                "sort": "created",
                "direction": "desc",
                "per_page": per_page,
                "page": page,
                **({"base": base} if base else {}),
            },
        )
        if not prs:
            break
        for pr in prs:
            if pr.get("merged_at"):
                out[int(pr["number"])] = pr_assignees(pr)
        if len(prs) < per_page:
            break
    return out


async def pr_files(repo: str, token: str, number: int, *, max_pages: int = 6) -> list[str]:
    """Changed file paths of one PR (capped at max_pages*100)."""
    files: list[str] = []
    for page in range(1, max_pages + 1):
        rows = await _get_list(
            f"{_API}/repos/{repo}/pulls/{number}/files",
            token,
            {"per_page": 100, "page": page},
        )
        if not rows:
            break
        files.extend(str(r.get("filename") or "") for r in rows)
        if len(rows) < 100:
            break
    return [f for f in files if f]
