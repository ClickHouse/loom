"""`code.bisect_candidates` `deadline_ms`: the optional stages (fix blame, marks, the
duplicate check's wait, the rerank) wait no longer than what is left and are skipped
when too little is. The answer carries the candidates found so far with
`partial` / `partial_reason`, and a call without a deadline is unchanged."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.code.app import bisect as cb
from server.code.app import bisect_facts, bisect_fix
from server.tracker.domain import bisect as bi


def _pool() -> list[bi.Candidate]:
    return [bi.Candidate(pr_number=1, title="a", author="", merged_at="", minor_version="")]


def _with_deadline(seconds: float | None) -> tuple[Any, Any]:
    end = None if seconds is None else time.monotonic() + seconds
    return bisect_facts._DEADLINE.set(end), bisect_facts._CUT.set([])


def _reset(tokens: tuple[Any, Any]) -> None:
    bisect_facts._DEADLINE.reset(tokens[0])
    bisect_facts._CUT.reset(tokens[1])


def test_without_a_deadline_every_stage_keeps_its_own_budget() -> None:
    tokens = _with_deadline(None)
    try:
        assert bisect_facts._budget(8.0) == 8.0
        assert bisect_facts._short(bisect_facts.RERANK_MIN_S, "rerank") is False
        assert bisect_facts._expired("fix_blame") is False
        assert bisect_facts._CUT.get() == []
    finally:
        _reset(tokens)


def test_a_deadline_bounds_a_stage_and_names_the_ones_it_cuts() -> None:
    tokens = _with_deadline(1.0)
    try:
        assert 0.9 < bisect_facts._budget(8.0) <= 1.0
        assert bisect_facts._budget(0.2) == 0.2, "a stage's own budget wins when it is shorter"
        assert bisect_facts._short(bisect_facts.RERANK_MIN_S, "rerank") is True
        assert bisect_facts._short(bisect_facts.STAGE_MIN_S, "marks") is False
        assert bisect_facts._CUT.get() == ["rerank"]
    finally:
        _reset(tokens)


def test_fix_blame_is_skipped_when_too_little_is_left() -> None:
    class Repo:
        async def merge_commit_of(self, org: str, ns: str, pr: int) -> str:
            raise AssertionError("not reached: the stage never starts")

    tokens = _with_deadline(0.1)
    try:
        out, summary = asyncio.run(bisect_fix._fix_blame_stage(Repo(), "o", "n", _pool(), 5))  # type: ignore[arg-type]
        assert out == _pool() and summary == {"fix_pr": 5, "skipped": "deadline"}
        assert bisect_facts._CUT.get() == ["fix_blame"]
    finally:
        _reset(tokens)


def test_a_slow_fix_blame_stops_at_the_deadline_not_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import blame as code_blame
    from server.code.app.index import gitrepo
    from server.code.domain.index import fix_blame as fb

    class Repo:
        async def merge_commit_of(self, org: str, ns: str, pr: int) -> str:
            return "m" * 40

    async def run(git_dir: str, *args: str) -> str:
        return "p" * 40 if args[0] == "rev-parse" else "diff"

    async def slow_blame(inp: Any) -> Any:
        await asyncio.sleep(30)

    monkeypatch.setattr(gitrepo, "run", run)
    monkeypatch.setattr(code_blame, "repo_siblings", lambda org, ns: (None, None))
    monkeypatch.setattr(code_blame, "handle_code_blame_range", slow_blame)
    monkeypatch.setattr(
        fb, "pre_image_ranges", lambda diff: [SimpleNamespace(path="src/a.cpp", start=1, end=2)]
    )
    tokens = _with_deadline(0.7)
    try:
        t0 = time.monotonic()
        out, summary = asyncio.run(bisect_fix._fix_blame_stage(Repo(), "o", "n", _pool(), 5))  # type: ignore[arg-type]
        assert time.monotonic() - t0 < 1.5, "answered at the deadline, not after 8 s"
        assert out == _pool() and summary["skipped"] == "deadline"
        assert bisect_facts._CUT.get() == ["fix_blame"]
    finally:
        _reset(tokens)


def test_the_rerank_is_skipped_without_calling_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("the rerank must not run")

    monkeypatch.setattr(cb, "rerank_stage", never)
    monkeypatch.setattr(cb, "wide_extras", never)
    tokens = _with_deadline(0.5)
    try:
        pool, rerank = asyncio.run(
            cb._second_reading(
                SimpleNamespace(rerank=False, paths=[]),
                None,
                "o",
                "t",
                _pool(),
                {"weak": True},
                {},
                facts=SimpleNamespace(),  # type: ignore[arg-type]
                plan=SimpleNamespace(),  # type: ignore[arg-type]
                report_text="report",
                report_at=None,
            )
        )
        assert pool == _pool()
        assert rerank == {"status": "skipped", "reason": "deadline", "confidence": "low"}
        assert bisect_facts._CUT.get() == ["rerank"]
    finally:
        _reset(tokens)


def test_deadline_ms_is_bounded_like_code_search() -> None:
    from server.code.api.models_history import CodeBisectCandidatesInput

    assert CodeBisectCandidatesInput(org="o", namespace="n", issue_number=1).deadline_ms is None
    assert (
        CodeBisectCandidatesInput(org="o", namespace="n", issue_number=1, deadline_ms=0).deadline_ms
        == 0
    )
    with pytest.raises(ValidationError):
        CodeBisectCandidatesInput(org="o", namespace="n", issue_number=1, deadline_ms=60001)


def test_partial_is_on_the_wire_only_when_something_was_cut() -> None:
    from server.code.api.models_history import CodeBisectCandidatesOutput

    whole = CodeBisectCandidatesOutput(verdict="unknown").model_dump()
    assert "partial" not in whole and "partial_reason" not in whole
    cut = CodeBisectCandidatesOutput(
        verdict="unknown", partial=True, partial_reason="deadline_ms cut: fix_blame, rerank"
    ).model_dump()
    assert cut["partial"] is True and cut["partial_reason"].endswith("fix_blame, rerank")
