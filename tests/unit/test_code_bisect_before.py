# ruff: noqa: F811 — the `wired` fixture is imported from test_code_bisect and named in signatures
"""code.bisect_candidates ranks without a release pair — a
merge-time window (`before`, optional `after`) or a known-bad commit
(`bad_sha`, resolved on the mirror) bounds the QA scan when no `bad_version`
exists, which is the shape a reviewer has when a bug surfaced while reading
a PR and no release has carried it yet."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import server.code.app.bisect as h
from server.code.api.models_history import CodeBisectCandidatesInput
from server.code.domain import time_window
from server.core.errors import ValidationError
from server.tracker.domain import bisect as bi
from tests.unit.test_code_bisect import NS, QA, _Source, wired  # noqa: F401 — fixture reuse

pytestmark = pytest.mark.usefixtures("mirror_here")

ORG = "clickhouse"
PR = {
    "repo": "ClickHouse/ClickHouse",
    "pr_number": 555,
    "title": "Refactor StorageLog reads",
    "author": "a",
    "merged_at": datetime(2026, 9, 14, 12, 0),
    "minor_version": "",
    "has_test": 1,
    "is_backport_pr": 0,
    "url": "",
    "additions": 10,
    "deletions": 2,
    "matched_paths": ["src/Storages/StorageLog.cpp"],
    "matched_symbols": [],
    "matched_patterns": [],
    "added_patterns": [],
    "hunk_symbols": [],
}


def _inp(**kw: Any) -> CodeBisectCandidatesInput:
    base = {"org": ORG, "namespace": NS, "paths": ["src/Storages/StorageLog.cpp"]}
    base.update(kw)
    return CodeBisectCandidatesInput(**base)


@pytest.mark.asyncio
async def test_before_bounds_the_scan_without_versions(wired) -> None:
    _repo, use = wired
    src = use(_Source(prs=[PR]))
    out = await h.handle_code_bisect_candidates(_inp(before="2026-09-15T14:07:00Z"))
    call = src.pr_calls[-1]
    assert call["versions"] == [] and call["before"] == datetime(2026, 9, 15, 14, 7)
    assert "no_failing_version" not in out.causes
    assert out.window == "merged_before" and out.before == "2026-09-15T14:07:00Z"
    assert [c["pr_number"] for c in out.candidates] == [555]


@pytest.mark.asyncio
async def test_after_is_the_lower_bound_and_must_precede_before(wired) -> None:
    _repo, use = wired
    src = use(_Source(prs=[PR]))
    await h.handle_code_bisect_candidates(_inp(before="2026-09-15 14:07", after="2026-09-01"))
    assert src.pr_calls[-1]["after"] == datetime(2026, 9, 1)
    with pytest.raises(ValidationError) as ei:
        await h.handle_code_bisect_candidates(_inp(before="2026-09-01", after="2026-09-15"))
    assert ei.value.to_detail()["code"] == "BAD_TIME"


@pytest.mark.asyncio
async def test_bad_sha_resolves_to_the_commit_time_on_the_mirror(wired, monkeypatch) -> None:
    from types import SimpleNamespace

    import server.code.app.index.gitrepo as gitrepo
    import server.code.domain.index.repos as repos

    _repo, use = wired
    src = use(_Source(prs=[PR]))
    monkeypatch.setattr(
        repos, "repo_for_namespace", lambda org, ns: SimpleNamespace(git_dir="/m.git")
    )

    async def _commit_time(git_dir: str, sha: str) -> int:
        if sha != "abc1234":
            raise RuntimeError("unknown revision")
        return int(datetime(2026, 9, 15, 14, 7, tzinfo=UTC).timestamp())

    monkeypatch.setattr(gitrepo, "commit_time", _commit_time)
    out = await h.handle_code_bisect_candidates(_inp(bad_sha="abc1234"))
    assert src.pr_calls[-1]["before"] == datetime(2026, 9, 15, 14, 7)
    assert out.before == "2026-09-15T14:07:00Z"
    with pytest.raises(ValidationError) as ei:
        await h.handle_code_bisect_candidates(_inp(bad_sha="deadbeef"))
    assert ei.value.to_detail()["code"] == "BAD_SHA"


@pytest.mark.asyncio
async def test_release_versions_still_win_over_a_time_window(wired) -> None:
    _repo, use = wired
    src = use(_Source(prs=[PR]))
    out = await h.handle_code_bisect_candidates(
        _inp(bad_version="26.8", good_version="26.6", before="2026-09-15T14:07:00Z")
    )
    call = src.pr_calls[-1]
    assert call["versions"] and call["before"] is None
    assert out.window != "merged_before" and out.before == ""


@pytest.mark.asyncio
async def test_no_window_at_all_still_says_no_failing_version(wired) -> None:
    _repo, use = wired
    use(_Source(prs=[PR]))
    out = await h.handle_code_bisect_candidates(_inp())
    assert out.verdict == "unknown" and "no_failing_version" in out.causes


def test_bad_time_is_a_sentence() -> None:
    with pytest.raises(ValidationError) as ei:
        time_window._parse_when("yesterday-ish")
    assert ei.value.to_detail()["code"] == "BAD_TIME"
    assert time_window._parse_when("2026-09-15T14:07:00+02:00") == datetime(2026, 9, 15, 12, 7)


def test_helpers_know_the_time_window() -> None:
    assert bi.window_name("", "", [], before=True) == "merged_before"
    assert bi.window_name("", "", []) == ""
    v, causes = bi.verdict_of(
        candidates=[],
        versions=[],
        paths=["p"],
        introduced_commit="",
        qa_ok=True,
        graph_named=False,
        widened=False,
        time_window=True,
    )
    assert "no_failing_version" not in causes


@pytest.mark.asyncio
async def test_source_scan_uses_closed_at_when_no_versions(monkeypatch) -> None:
    from server.tracker.store.source import TrackerSource

    src = TrackerSource.__new__(TrackerSource)
    src.db = "qa"
    captured: dict[str, Any] = {}

    async def _rows(
        tag, sql, params, *, cache_key, budget_ms=None, ttl_s=None, query_settings=None
    ):
        captured["sql"], captured["params"] = sql, params
        return []

    monkeypatch.setattr(src, "_rows", _rows)
    monkeypatch.setattr(src, "repos", lambda: ["ClickHouse/ClickHouse"])
    monkeypatch.setattr(src, "_privacy", lambda: "")
    await src.merged_prs_touching(
        ORG, QA, ["src/a.cpp"], [], before=datetime(2026, 9, 15, 14, 7), after=datetime(2026, 9, 1)
    )
    assert (
        "closed_at <= {before:DateTime}" in captured["sql"]
        and "closed_at > {after:DateTime}" in captured["sql"]
    )
    assert "minor_version IN" not in captured["sql"]
    assert captured["params"]["before"] == "2026-09-15 14:07:00"
    await src.merged_prs_touching(ORG, QA, ["src/a.cpp"], ["26.8"])
    assert "minor_version IN {versions:Array(String)}" in captured["sql"]
    assert await src.merged_prs_touching(ORG, QA, ["src/a.cpp"], []) == [], (
        "no versions and no time → nothing"
    )
