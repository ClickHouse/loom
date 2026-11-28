"""Forgejo REST API client for the code plane's merged-PR history.

The counterpart of `github.merged_prs_since` for a repository on a Forgejo host: merged
PRs with their files and reviewers (`loom.code_pr_history`). It returns `github`'s
`MergedPR` and raises its errors (`GitHubError`, `RateLimited` for a 429), so the worker
keeps one "stop, cursor unmoved" path for both sources.

`repo` is the API URL of the repository (`remote.forgejo_api_repo`). Where the API
differs from GitHub's:

- Pages follow the `Link: rel="next"` header; the server caps a page at its
  `MAX_RESPONSE_ITEMS` (50 by default) whatever `limit` asks for.
- Timestamps carry the server's UTC offset; they are converted to naive UTC.
- `/pulls` has no `base` filter: the target branch is filtered here, on `base.ref`.
- There is no merged-date search: the backfill walks closed PRs by close time
  (`sort=recentclose`, Forgejo 12 or later).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from server.clients._http import get_client
from server.clients.github import (
    GitHubError,
    MergedPR,
    RateLimited,
    pr_assignees,
    pr_base_ref,
)

# Forgejo caps a page at its own MAX_RESPONSE_ITEMS (50 by default), so a larger value
# would change nothing.
_PAGE_SIZE = 50  # not a setting: the server's own cap


def _headers(token: str) -> dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": "loom-code-intel/1.0"}
    if token:
        h["Authorization"] = f"token {token}"
    return h


def _has_next(resp: httpx.Response) -> bool:
    return 'rel="next"' in resp.headers.get("Link", "")


async def _get_page(
    url: str, token: str, params: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """One list page's objects and whether the server names a next page. Every failure
    is a `GitHubError` (a 429 its `RateLimited` subclass)."""
    try:
        resp = await get_client().get(url, headers=_headers(token), params=params)
    except httpx.RequestError as e:
        raise GitHubError(f"GET {url} -> {type(e).__name__}") from e
    if resp.status_code == 429:
        raise RateLimited(f"rate limited; retry after {resp.headers.get('Retry-After', '?')}s")
    if resp.status_code >= 400:
        raise GitHubError(f"GET {url} -> {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as e:
        raise GitHubError(f"GET {url} -> {resp.status_code}: body is not JSON") from e
    if not isinstance(body, list):
        raise GitHubError(f"GET {url} -> expected a JSON array, got {type(body).__name__}")
    return [item for item in body if isinstance(item, dict)], _has_next(resp)


def _parse_dt(s: str | None) -> datetime | None:
    """An RFC 3339 timestamp with any offset as naive UTC."""
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.replace(tzinfo=None)


async def pr_files(repo: str, token: str, number: int, *, max_pages: int = 12) -> list[str]:
    """Changed file paths of one PR (capped at max_pages pages)."""
    files: list[str] = []
    for page in range(1, max_pages + 1):
        rows, more = await _get_page(
            f"{repo}/pulls/{int(number)}/files", token, {"limit": _PAGE_SIZE, "page": page}
        )
        files.extend(str(r.get("filename") or "") for r in rows)
        if not more:
            break
    return [f for f in files if f]


async def _hydrate_pr(
    repo: str, token: str, pr: dict[str, Any], merged: datetime, updated: datetime | None
) -> MergedPR:
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
        files=await pr_files(repo, token, int(pr["number"])),
        assignees=pr_assignees(pr),
        body=str(pr.get("body") or ""),
        target_branch=pr_base_ref(pr),
    )


def _merged_into(pr: dict[str, Any], base: str) -> datetime | None:
    """merged_at of a PR merged into `base` ('' = any branch); None otherwise."""
    merged = _parse_dt(pr.get("merged_at")) if pr.get("merged") is not False else None
    if merged is None or (base and pr_base_ref(pr) != base):
        return None
    return merged


async def merged_prs_since(
    repo: str,
    token: str,
    since: datetime | None,
    *,
    max_prs: int = 50,
    base: str = "",
) -> list[MergedPR]:
    """PRs merged into `base` and updated after `since`, with files, oldest-updated
    first: closed PRs newest update first (`sort=recentupdate`), stopping at the first
    one at or before `since`, at most 10 pages, as `github.merged_prs_since` walks."""
    collected: list[MergedPR] = []
    page = 1
    while len(collected) < max_prs and page <= 10:
        prs, more = await _get_page(
            f"{repo}/pulls",
            token,
            {"state": "closed", "sort": "recentupdate", "limit": _PAGE_SIZE, "page": page},
        )
        drained = False
        for pr in prs:
            updated = _parse_dt(pr.get("updated_at"))
            if since is not None and updated is not None and updated <= since:
                drained = True
                break
            merged = _merged_into(pr, base)
            if merged is None:
                continue
            collected.append(await _hydrate_pr(repo, token, pr, merged, updated))
            if len(collected) >= max_prs:
                break
        if drained or not more:
            break
        page += 1
    collected.sort(key=lambda p: p.updated_at)
    return collected


async def merged_prs_back_to(
    repo: str,
    token: str,
    oldest: datetime,
    *,
    max_prs: int = 1000,
    base: str = "",
    start_page: int = 1,
    max_pages: int = 20,
) -> tuple[list[MergedPR], int]:
    """PRs merged into `base` and closed at or after `oldest`, newest close first
    (`sort=recentclose`), from `start_page` on; returns them with the page to resume
    at, 0 once the walk reached `oldest` or the last page. A page is read whole, so
    `max_prs` is checked between pages and a resume never repeats a PR. The
    deep-history backfill: the API has no merged-date search, and close time is merge
    time for a merged PR. `sort=recentclose` needs Forgejo 12 or later; older servers
    ignore it and answer in creation order."""
    out: list[MergedPR] = []
    page = max(1, start_page)
    for _ in range(max_pages):
        prs, more = await _get_page(
            f"{repo}/pulls",
            token,
            {"state": "closed", "sort": "recentclose", "limit": _PAGE_SIZE, "page": page},
        )
        for pr in prs:
            closed = _parse_dt(pr.get("closed_at"))
            if closed is not None and closed < oldest:
                return out, 0
            merged = _merged_into(pr, base)
            if merged is None:
                continue
            out.append(await _hydrate_pr(repo, token, pr, merged, _parse_dt(pr.get("updated_at"))))
        if not more:
            return out, 0
        page += 1
        if len(out) >= max_prs:
            return out, page
    return out, page
