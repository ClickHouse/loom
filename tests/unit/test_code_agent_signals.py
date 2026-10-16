"""Agent-facing signals: as_of freshness (lag_commits/stale), not_covered for
paths outside the indexed prefixes, index-error and recent-commit listings,
and anonymous PR-history sync."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app import common as code_common
from server.code.app import grep, index_status
from server.code.app.search import handler as search_mod
from server.code.app.ttl_cache import clear_all
from server.code.domain.index.repos import RepoConfig
from server.settings import settings

pytestmark = pytest.mark.usefixtures("mirror_here")

CFG = RepoConfig(
    org="clickhouse",
    namespace="code-clickhouse",
    repo="ClickHouse/ClickHouse",
    git_dir="/nope/clickhouse.git",
    git_remote="r",
    git_branch="master",
    path_prefixes=("src/", "base/", "tests/"),
    github_token="",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "clickhouse")
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(settings, "code_stale_after_commits", 5)
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(
        repos, "repo_for_namespace", lambda o, n: CFG if n == "code-clickhouse" else None
    )
    clear_all()


class _Repo:
    async def all_state(self, org, ns):
        return {"git_head": {"value": "a" * 40, "updated_at": None}}


# ---- as_of freshness ----------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_as_of_cache():
    """index_as_of is served from a 30 s per-namespace cache;
    each test computes its own block."""
    code_common._AS_OF_CACHE.clear()
    yield
    code_common._AS_OF_CACHE.clear()


@pytest.mark.asyncio
async def test_as_of_carries_lag_and_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.code.app.index.gitrepo as gitrepo

    calls = {"n": 0}

    async def _tip(git_dir, branch):
        calls["n"] += 1
        return "b" * 40

    async def _count(git_dir, since, until):
        return 7

    monkeypatch.setattr(gitrepo, "head_sha", _tip)
    monkeypatch.setattr(gitrepo, "commit_count_between", _count)
    a = await code_common.index_as_of(_Repo(), "clickhouse", "code-clickhouse")
    assert a["commit"] == "a" * 40 and a["lag_commits"] == 7 and a["stale"] is True
    b = await code_common.index_as_of(_Repo(), "clickhouse", "code-clickhouse")
    assert b["lag_commits"] == 7 and calls["n"] == 1  # 60s cache: one git call


@pytest.mark.asyncio
async def test_as_of_unknown_lag_is_not_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.code.app.index.gitrepo as gitrepo

    async def _boom(git_dir, branch):
        raise RuntimeError("no clone here")

    monkeypatch.setattr(gitrepo, "head_sha", _boom)
    a = await code_common.index_as_of(_Repo(), "clickhouse", "code-clickhouse")
    assert a["lag_commits"] == -1 and a["stale"] is False


# ---- path coverage / not_covered ------------------------------------------


def test_path_coverage_semantics() -> None:
    assert code_common.path_coverage(CFG, "")[0] is True
    assert code_common.path_coverage(CFG, "src/Storages")[0] is True
    assert code_common.path_coverage(CFG, "src")[0] is True  # ancestor of an indexed prefix
    assert code_common.path_coverage(CFG, "./tests/queries/")[0] is True
    assert code_common.path_coverage(CFG, "docs/en")[0] is False
    assert code_common.path_coverage(CFG, "contrib/llvm")[0] is False


@pytest.mark.asyncio
async def test_search_outside_prefixes_abstains_not_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_search import CodeSearchInput

    monkeypatch.setattr(search_mod, "CodeRepo", _Repo)

    async def _as_of(repo, org, ns):
        return {"commit": "a" * 40, "lag_commits": 0, "stale": False}

    monkeypatch.setattr(search_mod, "index_as_of", _as_of)
    out = await search_mod.handle_code_search(
        CodeSearchInput(query="anything", path_prefix="docs/en/sql-reference")
    )
    assert out.abstain and out.abstain_code == "not_covered" and out.route == "not_covered"
    assert out.results == [] and out.retrieval_risk == "abstain"
    assert "src/" in out.abstain_reason and out.as_of["commit"] == "a" * 40


@pytest.mark.asyncio
async def test_grep_outside_prefixes_flags_not_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_grep import CodeGrepInput

    monkeypatch.setattr(grep, "CodeRepo", _Repo)

    async def _as_of(repo, org, ns):
        return {}

    monkeypatch.setattr(grep, "index_as_of", _as_of)
    out = await grep.handle_code_grep(CodeGrepInput(pattern="foo", path_prefix="docs/"))
    assert out.not_covered is True and out.matches == [] and out.chunks_matched == 0
    assert "docs/" in out.note


# ---- index_errors / recent_commits ----------------------------------------


class _CtxRepo:
    async def index_errors(self, org, ns, *, limit):
        return (
            [
                {
                    "path": "src/Gen.cpp",
                    "lang": "cpp",
                    "index_error": "RecursionError",
                    "indexed_commit": b"c" * 40,
                    "created_at": "2026-09-08 22:00:00",
                }
            ],
            1,
        )

    async def recent_indexed_commits(self, org, ns, *, limit):
        return [
            {
                "commit": b"d" * 40,
                "files": 3,
                "ingested_at": "2026-09-09 01:00:00",
                "pr_number": 118494,
            }
        ]


@pytest.mark.asyncio
async def test_index_errors_and_recent_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.code.app.index.gitrepo as gitrepo
    from server.code.api.models_status import CodeIndexErrorsInput, CodeRecentCommitsInput

    monkeypatch.setattr(index_status, "CodeRepo", _CtxRepo)

    # One git log decorates every row
    async def _meta_many(git_dir, shas):
        return {s: ("Fix the thing (#118494)", 1757372400) for s in shas}

    monkeypatch.setattr(gitrepo, "commit_meta_many", _meta_many)
    e = await index_status.handle_code_index_errors(CodeIndexErrorsInput())
    assert e.total == 1 and e.rows[0].path == "src/Gen.cpp" and e.rows[0].indexed_commit == "c" * 40
    r = await index_status.handle_code_recent_commits(CodeRecentCommitsInput())
    row = r.rows[0]
    assert row.commit == "d" * 40 and row.pr_number == 118494 and row.files == 3
    assert row.subject.startswith("Fix the thing") and row.committed_at.startswith("2025-09-0")


# ---- PR history without a token ------------------------------------------


@pytest.mark.asyncio
async def test_pr_history_syncs_anonymously_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.workers import pr_history as w

    seen: dict[str, Any] = {}

    async def _merged(repo, token, since, max_prs=40, base=""):
        seen["token"] = token
        seen["base"] = base
        return []

    import server.clients.github as gh

    monkeypatch.setattr(gh, "merged_prs_since", _merged)

    class _R:
        async def get_state(self, org, ns, key):
            return ""

    res = w.CodePrHistoryResult()
    await w._sync_repo(CFG, _R(), res)
    assert seen["token"] == ""  # went to GitHub anonymously instead of no-op
    assert res.note != "no token"


@pytest.mark.asyncio
async def test_pr_history_skips_every_github_leg_for_other_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.domain.index.repos as repos
    from server.code.workers import pr_history as w

    cfg = RepoConfig(
        org="dev",
        namespace="code-app",
        repo="gitlab.example/acme/app",
        git_dir="/tmp/app.git",
        git_remote="https://gitlab.example/acme/app.git",
        git_branch="main",
        path_prefixes=(),
        github_token="",
    )
    monkeypatch.setattr(repos, "configured_repos", lambda: [cfg])
    monkeypatch.setattr(w, "_sync_repo", lambda *_args: pytest.fail("GitHub sync called"))
    monkeypatch.setattr(w, "_sync_items", lambda *_args: pytest.fail("GitHub items called"))
    monkeypatch.setattr(w, "_fill_pr_heads", lambda *_args: pytest.fail("GitHub heads called"))

    result = w.CodePrHistoryResult()
    await w._run(result)
    assert result.note == "PR history is not available for git remotes"
