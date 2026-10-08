"""The Code page's reads stay cheap.

`code.recent_commits` decorates all rows with one `git log --no-walk` instead of
two git subprocesses per row. `code.coverage` remembers its scans of the
namespace's chunks, symbols and edges while the writers' watermarks match,
instead of scanning them under FINAL on every call."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app import index_status
from server.code.app.index import gitrepo

_STATE = {
    "git_head": {"value": "a" * 40, "detail": "", "updated_at": ""},
    "scip_commit": {"value": "", "detail": "", "updated_at": ""},
}


class _FakeRepo:
    calls = {"coverage": 0, "gaps": 0, "recent": 0}
    state = dict(_STATE)

    async def all_state(self, org: str, ns: str) -> dict[str, dict[str, Any]]:
        return dict(_FakeRepo.state)

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return str((_FakeRepo.state.get(key) or {}).get("value") or "")

    async def coverage(self, org: str, ns: str) -> dict[str, Any]:
        _FakeRepo.calls["coverage"] += 1
        return {
            "files": [{"lang": "py", "files": 3, "errored": 0, "first_at": None, "last_at": None}],
            "chunks": [],
            "symbols": [],
            "edges": [],
            "misc": {},
        }

    async def coverage_gaps(self, org: str, ns: str) -> dict[str, int]:
        _FakeRepo.calls["gaps"] += 1
        return {
            "functions_scanned": 10,
            "functions_uncalled": 2,
            "source_files": 3,
            "source_files_untested": 1,
            "code_files": 3,
            "code_files_no_symbols": 0,
        }

    async def recent_indexed_commits(
        self, org: str, ns: str, *, limit: int
    ) -> list[dict[str, Any]]:
        _FakeRepo.calls["recent"] += 1
        return [
            {"commit": "a" * 40, "ingested_at": None, "files": 2, "pr_number": 0},
            {"commit": "b" * 40, "ingested_at": None, "files": 1, "pr_number": 7},
            {"commit": "c" * 40, "ingested_at": None, "files": 1, "pr_number": 0},
        ]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    from server.settings import settings

    index_status.reset_repo_status_cache()
    _FakeRepo.calls = {"coverage": 0, "gaps": 0, "recent": 0}
    _FakeRepo.state = dict(_STATE)
    monkeypatch.setattr(index_status, "CodeRepo", _FakeRepo)
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-z")
    monkeypatch.setattr(settings, "code_repo", "acme/code-z")
    monkeypatch.setattr(settings, "code_git_dir", "var/code-z.git")
    monkeypatch.setattr(settings, "code_git_branch", "main")
    monkeypatch.setattr(settings, "code_path_prefixes", "")
    monkeypatch.setattr(settings, "code_github_token", "")
    yield
    index_status.reset_repo_status_cache()


@pytest.mark.asyncio
async def test_recent_commits_decorates_every_row_with_one_git_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_status import CodeRecentCommitsInput

    asked: list[list[str]] = []

    async def _many(git_dir: str, shas: list[str]) -> dict[str, tuple[str, int]]:
        asked.append(list(shas))
        return {
            "a" * 40: ("first", 1_700_000_000),
            "b" * 40: ("second", 1_700_000_100),
        }  # c is unknown

    async def _never(*_: Any) -> Any:
        raise AssertionError("per-row git calls must not run")

    monkeypatch.setattr(gitrepo, "commit_meta_many", _many)
    monkeypatch.setattr(gitrepo, "commit_subject", _never)
    monkeypatch.setattr(gitrepo, "commit_time", _never)
    out = await index_status.handle_code_recent_commits(
        CodeRecentCommitsInput(org="acme", namespace="code-z", limit=3)
    )
    assert asked == [["a" * 40, "b" * 40, "c" * 40]], "one call carries every sha"
    assert [r.subject for r in out.rows] == ["first", "second", ""]
    assert out.rows[0].committed_at.startswith("2023-11-14") and out.rows[2].committed_at == ""
    assert out.rows[1].pr_number == 7


@pytest.mark.asyncio
async def test_coverage_is_remembered_while_the_watermarks_match() -> None:
    from server.code.api.models_status import CodeCoverageInput

    inp = CodeCoverageInput(org="acme", namespace="code-z")
    out1 = await index_status.handle_code_coverage(inp)
    out2 = await index_status.handle_code_coverage(inp)
    assert _FakeRepo.calls["coverage"] == 1 and _FakeRepo.calls["gaps"] == 1
    assert out1.gaps is not None and out1.gaps.uncalled_share == 0.2
    assert out2.languages == out1.languages
    # the index moved: the next call scans again
    _FakeRepo.state = {
        **_FakeRepo.state,
        "git_head": {"value": "d" * 40, "detail": "", "updated_at": ""},
    }
    out3 = await index_status.handle_code_coverage(inp)
    assert _FakeRepo.calls["coverage"] == 2 and _FakeRepo.calls["gaps"] == 2
    assert out3.as_of_commit == "d" * 40


@pytest.mark.asyncio
async def test_a_failed_gap_query_is_remembered_as_no_gaps_not_retried_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_status import CodeCoverageInput

    async def _boom(self: Any, org: str, ns: str) -> dict[str, int]:
        _FakeRepo.calls["gaps"] += 1
        raise RuntimeError("no gap tables")

    monkeypatch.setattr(_FakeRepo, "coverage_gaps", _boom)
    inp = CodeCoverageInput(org="acme", namespace="code-z")
    out1 = await index_status.handle_code_coverage(inp)
    out2 = await index_status.handle_code_coverage(inp)
    assert out1.gaps is None and out2.gaps is None
    assert _FakeRepo.calls["gaps"] == 1 and _FakeRepo.calls["coverage"] == 1
