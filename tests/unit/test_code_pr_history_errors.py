"""code_pr_history: GitHub errors must be logged and counted, not just appended.

`rec.outcome` must carry the errors, not only prs_upserted/cursor/note, so a
private repo answering 404 or a revoked token shows in the logs and in
/v1/admin/workers.status instead of passing silently.
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.workers import pr_history as w
from tests.unit.code_fixtures import repo_config


def _Cfg(*_a: Any, **_k: Any) -> Any:
    return repo_config(org="o", namespace="code-x", repo="Org/Repo", github_token="tok")


@pytest.mark.asyncio
async def test_github_error_is_logged_and_counted(caplog: pytest.LogCaptureFixture) -> None:
    from server.clients.github import GitHubError
    from server.workers.status import get_registry

    get_registry().reset()
    repo = AsyncMock()

    async def _state(org: str, ns: str, key: str) -> str:
        return "a" * 40 if key == "git_head" else ""  # indexed; no PR cursor yet

    repo.get_state.side_effect = _state

    async def _fail(*_: Any, **__: Any) -> Any:
        raise GitHubError("404 Not Found: Org/Repo")

    async def _none(*_: Any, **__: Any) -> Any:
        return []  # the items sync must not reach GitHub from a unit test

    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
        patch("server.clients.github.merged_prs_since", _fail),
        patch("server.clients.github.github_items_since", _none),
        caplog.at_level(logging.WARNING, logger="loom.workers.code_pr_history"),
    ):
        result = await w.run_pass()

    assert result.errors == ["o/code-x: 404 Not Found: Org/Repo"]
    # The error text reaches the log — once at the append site, once in the
    # per-pass summary.
    texts = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("github error" in t and "404 Not Found" in t for t in texts)
    assert any("1 error(s) this pass" in t for t in texts)
    # ...and the count reaches the status registry.
    snap = get_registry().snapshot()["code_pr_history"]
    assert snap["last_outcome"]["errors"] == 1
    # Cursor untouched: pass over, never a gap.
    repo.set_state.assert_not_called()


def test_pr_assignees_union_requested_minus_author_and_dupes() -> None:
    from server.clients.github import pr_assignees

    pr = {
        "user": {"login": "Author"},
        "assignees": [{"login": "rev1"}, {"login": "author"}],
        "requested_reviewers": [{"login": "Rev1"}, {"login": "rev2"}, {}],
    }
    assert pr_assignees(pr) == ["rev1", "rev2"]
    assert pr_assignees({"user": {"login": "a"}}) == []


def test_parse_gh_item_marks_prs_and_reads_state_dates() -> None:
    from server.clients.github import parse_gh_item

    issue = parse_gh_item(
        {
            "number": 118000,
            "state": "closed",
            "state_reason": "not_planned",
            "user": {"login": "alice"},
            "title": "t",
            "labels": [{"name": "bug"}],
            "created_at": "2026-08-01T10:00:00Z",
            "updated_at": "2026-08-02T00:00:00Z",
            "closed_at": "2026-08-02T00:00:00Z",
        }
    )
    assert (
        issue
        and not issue.is_pr
        and issue.state_reason == "not_planned"
        and issue.labels == ["bug"]
    )
    pr = parse_gh_item(
        {
            "number": 118001,
            "state": "closed",
            "user": {"login": "bob"},
            "created_at": "2026-08-01T10:00:00Z",
            "updated_at": "2026-08-03T00:00:00Z",
            "closed_at": "2026-08-03T00:00:00Z",
            "pull_request": {"merged_at": "2026-08-03T00:00:00Z"},
        }
    )
    assert pr and pr.is_pr and pr.merged_at is not None
    assert parse_gh_item({"number": 0}) is None


@pytest.mark.asyncio
async def test_github_items_since_keeps_progress_when_a_later_page_is_rate_limited() -> None:
    from server.clients import github as gh

    calls: list[int] = []

    async def _fake_get(url: str, token: str, params: dict[str, Any] | None = None) -> Any:
        page = int((params or {}).get("page") or 1)
        calls.append(page)
        if page == 3:
            raise gh.RateLimited("secondary rate limit")
        return [
            {
                "number": page * 100 + i,
                "state": "open",
                "user": {"login": "u"},
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": f"2026-08-0{page}T00:00:00Z",
            }
            for i in range(100)
        ]

    with patch.object(gh, "_get", _fake_get):
        items = await gh.github_items_since("o/r", "tok", None, max_pages=10, pace_s=0)
    assert calls == [1, 2, 3] and len(items) == 200, "two full pages kept, the walk stops"
    assert max(i.updated_at for i in items).day == 2, "cursor can advance to the last kept page"

    async def _fail_first(url: str, token: str, params: dict[str, Any] | None = None) -> Any:
        raise gh.RateLimited("secondary rate limit")

    with patch.object(gh, "_get", _fail_first), pytest.raises(gh.RateLimited):
        await gh.github_items_since("o/r", "tok", None, max_pages=3, pace_s=0)


def test_items_cursor_is_positional_not_the_page_maximum() -> None:
    from datetime import datetime

    from server.clients.github import GhItem, items_cursor

    def it(n: int, upd: datetime) -> GhItem:
        return GhItem(n, False, "open", "", "u", "", [], upd, None, None, upd)

    base = datetime(2024, 10, 1, tzinfo=UTC)
    page = [it(i, base.replace(hour=i % 24, day=1 + i // 24)) for i in range(100)]
    page[57] = it(9999, datetime(2026, 5, 11, tzinfo=UTC))  # GitHub's out-of-order item
    cur = items_cursor(page, per_page=100)
    assert cur is not None and cur.year == 2024, (
        "an outlier must not drag the cursor 19 months ahead"
    )
    assert cur <= page[30].updated_at
    short = page[:40]
    assert items_cursor(short, per_page=100) == max(i.updated_at for i in short), (
        "tail reached: max"
    )
    assert items_cursor([], 100) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "body", "limited"),
    [
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"}, "{}", True),
        (429, {"X-RateLimit-Remaining": "0"}, "{}", True),
        (403, {"Retry-After": "60"}, "{}", True),
        (403, {}, '{"message": "You have exceeded a secondary rate limit."}', True),
        (403, {}, '{"message": "Resource not accessible by integration"}', False),
        (404, {}, '{"message": "Not Found"}', False),
    ],
)
async def test_get_classifies_every_github_rate_limit_shape(
    status: int, headers: dict[str, str], body: str, limited: bool
) -> None:
    """Primary exhaustion and the secondary limit arrive as 403 or 429; each
    must be a RateLimited so the callers stop with the cursor kept, instead of
    treating the refusal as a per-item error and moving on."""
    import httpx

    from server.clients import github as gh

    client = AsyncMock()
    client.get.return_value = httpx.Response(status, headers=headers, text=body)
    with patch.object(gh, "get_client", return_value=client), pytest.raises(gh.GitHubError) as exc:
        await gh._get("https://api.github.com/x", "tok")
    assert isinstance(exc.value, gh.RateLimited) is limited


def _three() -> list[Any]:
    return [
        repo_config(org="o", namespace=f"code-{n}", repo=f"Org/{n}", github_token="tok")
        for n in ("a", "b", "c")
    ]


class _Budget:
    """Exhausted from the `after`-th check on."""

    seconds = 675.0

    def __init__(self, after: int) -> None:
        self.after, self.checks = after, 0

    def exhausted(self) -> bool:
        self.checks += 1
        return self.checks > self.after


async def _pass_order(resume: str, budget: _Budget, *, hang_in: str = "") -> tuple[list[str], Any]:
    import asyncio

    from server.workers import pass_context

    seen: list[str] = []

    async def _sync(cfg: Any, *_: Any) -> None:
        seen.append(cfg.namespace)
        if cfg.namespace == hang_in:
            await asyncio.sleep(3600)

    async def _noop(*_: Any) -> None:
        return None

    ctx = pass_context.begin("code_pr_history", resume_cursor=resume)
    with (
        patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
        patch("server.code.domain.index.repos.configured_repos", return_value=_three()),
        patch.object(w, "_pass_budget", return_value=budget),
        patch.object(w, "_sync_repo", _sync),
        patch.object(w, "_sync_items", _noop),
        patch.object(w, "_fill_pr_heads", _noop),
    ):
        try:
            if hang_in:
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0.2):
                        await w._run(w.CodePrHistoryResult())
            else:
                await w._run(w.CodePrHistoryResult())
        finally:
            pass_context.clear()
    return seen, ctx


@pytest.mark.asyncio
async def test_pass_starts_at_the_entry_the_last_one_did_not_finish() -> None:
    seen, ctx = await _pass_order("code-b", _Budget(after=99))
    assert seen == ["code-b", "code-c", "code-a"]
    # A finished pass sends the next one back to the configured order's start.
    assert ctx.cursor == "code-a"


@pytest.mark.asyncio
async def test_budget_stops_the_pass_and_names_where_the_next_starts() -> None:
    seen, ctx = await _pass_order("", _Budget(after=1))
    assert seen == ["code-a"]
    assert ctx.cursor == "code-b"


@pytest.mark.asyncio
async def test_pass_cancelled_inside_an_entry_resumes_after_it() -> None:
    seen, ctx = await _pass_order("", _Budget(after=99), hang_in="code-b")
    assert seen == ["code-a", "code-b"]
    # One entry that outlasts the deadline must not stall the others.
    assert ctx.cursor == "code-c"
