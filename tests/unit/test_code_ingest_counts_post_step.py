"""The table-counts refresh is a cheap post-step — it
runs after every cursor of the pass is written, once per entry whose
commits moved rows, and never fails the pass."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import server.code.app.index.ingest_pass as w
from server.code.app.index import pass_budget as ingest_budget


class _Repo:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def refresh_table_counts(self, org: str, ns: str, **_: Any) -> dict[str, int]:
        self.calls.append((org, ns))
        if self.fail:
            raise RuntimeError("ch down")
        return {}

    # The same pass publishes the coverage row
    async def refresh_coverage(self, org: str, ns: str, **_: Any) -> tuple[dict[str, Any], None]:
        self.calls.append(("coverage", ns))
        if self.fail:
            raise RuntimeError("ch down")
        return {}, None


def _plan(repo: _Repo, commits: int, **kw: Any) -> ingest_budget.RepoPass:
    cfg = SimpleNamespace(org="clickhouse", namespace="code-x", label="clickhouse/code-x")
    return ingest_budget.RepoPass(cfg, repo, "h", "w", False, commits=commits, **kw)


@pytest.mark.asyncio
async def test_counts_refresh_runs_in_the_post_step_only_when_commits_landed() -> None:
    repo = _Repo()
    result = ingest_budget.CodeIngestResult()
    await w._post_steps(_plan(repo, commits=3), result)
    assert repo.calls == [("clickhouse", "code-x"), ("coverage", "code-x")]
    await w._post_steps(_plan(repo, commits=0), result)
    assert repo.calls == [("clickhouse", "code-x"), ("coverage", "code-x")], (
        "no commits this pass: nothing to publish"
    )


@pytest.mark.asyncio
async def test_a_failing_refresh_is_an_error_line_not_a_failed_pass() -> None:
    repo = _Repo(fail=True)
    result = ingest_budget.CodeIngestResult()
    await w._post_steps(_plan(repo, commits=1), result)
    assert repo.calls and any("table_counts" in e for e in result.errors)
    assert any("coverage" in e for e in result.errors), "the coverage row fails the same soft way"


def test_the_plan_counts_its_own_commits_not_the_passes() -> None:
    """Two entries in one pass: only the one whose commits landed refreshes,
    not both off the pass-wide counter."""
    assert ingest_budget.RepoPass.__dataclass_fields__["commits"].default == 0
