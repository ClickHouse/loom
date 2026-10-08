"""Coverage is published, not computed on open.

Computing `code.coverage` on open takes two FINAL aggregates over code_files
and code_chunks plus the gap counts (9 statements, over a GB cold on a large
repository), cold again on every open while a heal moves the counts
fingerprint. The ingest pass publishes the coverage beside its table counts as a
`code_index_state` row keyed by that fingerprint; the handler reads the row
and computes (and publishes) only when no row matches. The recent-commits
scan is remembered for a minute.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from server.code.app import index_status
from server.code.store import index as cr

_COV = {
    "files": [{"lang": "python", "files": 3, "errored": 0, "first_at": "t0", "last_at": "t1"}],
    "chunks": [],
    "symbols": [],
    "misc": {
        "pr_from": "",
        "pr_to": "",
        "pr_last_at": "",
        "pack_last_at": "",
        "summary_last_at": "",
    },
}
_GAPS = {"functions_scanned": 10, "functions_uncalled": 2}


class _Repo:
    """A repository whose scans must not run when a row answers."""

    def __init__(self) -> None:
        self.computed = 0
        self.published: list[tuple[str, str, str]] = []

    async def coverage(self, org: str, ns: str) -> dict[str, Any]:
        self.computed += 1
        return dict(_COV, misc=dict(_COV["misc"], pack_last_at=datetime(2026, 9, 19, 8, 0, 0)))

    async def coverage_gaps(self, org: str, ns: str) -> dict[str, int]:
        return dict(_GAPS)

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.published.append((key, value, detail))

    async def all_state(self, org: str, ns: str) -> dict[str, Any]:
        return {}

    # the real method, bound to this fake
    refresh_coverage = cr.CodeRepo.refresh_coverage


def _state(
    fp_parts: tuple[str, str, str], coverage_row: dict[str, Any] | None = None
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "git_head": {"value": fp_parts[0]},
        "scip_commit": {"value": fp_parts[1]},
        "scip_commit_py": {"value": fp_parts[2]},
    }
    if coverage_row is not None:
        state["coverage"] = coverage_row
    return state


@pytest.fixture(autouse=True)
def _fresh() -> None:
    index_status.reset_repo_status_cache()


@pytest.mark.asyncio
async def test_a_published_row_with_our_fingerprint_is_the_answer() -> None:
    repo = _Repo()
    state = _state(("h1", "s1", "p1"))
    fp = cr.counts_fingerprint(state)
    state["coverage"] = {"value": json.dumps({"cov": _COV, "gaps": _GAPS}), "detail": fp}
    cov, gaps = await index_status._coverage_cached(repo, "o", "n", state)
    assert cov == _COV and gaps == _GAPS
    assert repo.computed == 0 and repo.published == []


@pytest.mark.asyncio
async def test_no_row_computes_once_and_publishes_for_everyone() -> None:
    repo = _Repo()
    state = _state(("h1", "s1", "p1"))
    fp = cr.counts_fingerprint(state)
    cov, gaps = await index_status._coverage_cached(repo, "o", "n", state)
    assert repo.computed == 1 and gaps == _GAPS
    ((key, value, detail),) = repo.published
    assert key == "coverage" and detail == fp
    parsed = json.loads(value)
    assert parsed["gaps"] == _GAPS
    assert (
        parsed["cov"]["misc"]["pack_last_at"] == "2026-09-19T08:00:00"
    )  # a datetime travels as ISO text
    # the process memo answers the next open without a scan or a parse
    again, _ = await index_status._coverage_cached(repo, "o", "n", state)
    assert again["files"] == cov["files"] and repo.computed == 1


@pytest.mark.asyncio
async def test_a_stale_row_is_not_the_answer() -> None:
    repo = _Repo()
    state = _state(("h2", "s1", "p1"))
    state["coverage"] = {"value": json.dumps({"cov": _COV, "gaps": _GAPS}), "detail": "h1|s1|p1"}
    await index_status._coverage_cached(repo, "o", "n", state)
    assert repo.computed == 1  # the row was for another head


def test_parse_shared_coverage_tolerates_garbage() -> None:
    assert cr.parse_shared_coverage(None, "fp") is None
    assert cr.parse_shared_coverage({"value": "{not json", "detail": "fp"}, "fp") is None
    assert (
        cr.parse_shared_coverage({"value": json.dumps({"cov": []}), "detail": "fp"}, "fp") is None
    )
    cov, gaps = cr.parse_shared_coverage(
        {"value": json.dumps({"cov": _COV, "gaps": None}), "detail": "fp"}, "fp"
    ) or (None, None)
    assert cov == _COV and gaps is None


@pytest.mark.asyncio
async def test_recent_commits_are_remembered_for_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_recent(self: Any, org: str, ns: str, *, limit: int) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return [{"commit": "abc", "files": 3}]

    monkeypatch.setattr(cr.CodeRepo, "recent_indexed_commits", fake_recent)
    index_status._recent_commits_cache.clear()
    a = await index_status._recent_commits_cached("o", "n", 15)
    b = await index_status._recent_commits_cached("o", "n", 15)
    c = await index_status._recent_commits_cached("o", "n", 5)  # another limit is another answer
    assert a == b == [{"commit": "abc", "files": 3}] and calls == 2
    a[0]["files"] = 99  # a caller mutating its rows must not poison the memo
    assert (await index_status._recent_commits_cached("o", "n", 15))[0]["files"] == 3
    assert len(c) == 1


def test_the_ingest_pass_publishes_coverage_beside_its_counts() -> None:
    from server.code.app.index import ingest_pass as w

    src = open(w.__file__, encoding="utf-8").read()
    body = src[src.index("async def _post_steps(") :]
    body = body[: body.index("\nasync def ", 1)]
    assert "refresh_table_counts(" in body and "refresh_coverage(" in body
    assert body.index("refresh_table_counts(") < body.index("refresh_coverage(")
