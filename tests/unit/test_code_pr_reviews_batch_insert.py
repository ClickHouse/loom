"""The PR-history pass writes a batch's review rows in one insert."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _Repo:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, list(rows)))


@pytest.mark.asyncio
async def test_reviews_for_a_batch_of_prs_are_one_insert(monkeypatch) -> None:
    import server.clients.github as gh
    import server.code.store.pr_reviews_rows as prr
    from server.code.workers import pr_history as w

    async def comments(repo, token, n):
        if n == 2:
            raise gh.GitHubError("404")
        return [] if n == 3 else [{"id": n}]

    monkeypatch.setattr(gh, "pr_review_comments", comments)
    monkeypatch.setattr(
        prr, "review_rows", lambda org, ns, n, cs: [{"pr": n, "c": len(cs)}] if cs else [{"pr": n}]
    )
    repo = _Repo()
    cfg = SimpleNamespace(repo="o/r", github_token="t")
    written = await w._sync_reviews(cfg, repo, "org", "ns", [1, 2, 3, 4])
    assert written == 2
    # one statement; #2 failed and is skipped; #3's empty fetch keeps its marker row
    assert repo.inserts == [("code_pr_reviews", [{"pr": 1, "c": 1}, {"pr": 3}, {"pr": 4, "c": 1}])]


@pytest.mark.asyncio
async def test_a_rate_limit_writes_what_was_fetched(monkeypatch) -> None:
    import server.clients.github as gh
    import server.code.store.pr_reviews_rows as prr
    from server.code.workers import pr_history as w

    async def comments(repo, token, n):
        if n == 2:
            raise gh.RateLimited("slow down")
        return [{"id": n}]

    monkeypatch.setattr(gh, "pr_review_comments", comments)
    monkeypatch.setattr(prr, "review_rows", lambda org, ns, n, cs: [{"pr": n}])
    repo = _Repo()
    cfg = SimpleNamespace(repo="o/r", github_token="t")
    assert await w._sync_reviews(cfg, repo, "org", "ns", [1, 2, 3]) == 1
    assert repo.inserts == [("code_pr_reviews", [{"pr": 1}])]
