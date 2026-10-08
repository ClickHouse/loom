"""A `code.repos` refresh does not repeat work.

A cold listing would otherwise spend its time in Python and git, not
ClickHouse: per repository four git subprocesses and the same 14-day
worker-ledger query. The commit count between two fixed shas is immutable
history and is remembered; the ledger is read once per process per 15 s;
`code.index_status` reads the same cached status `code.repos` serves, on a
copy, instead of recomputing it."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app import index_status
from tests.unit.code_fixtures import repo_config

_STATE = {
    "git_head": {"value": "a" * 40, "detail": "", "updated_at": "2026-09-18 10:00:00"},
    "scip_commit": {"value": "b" * 40, "detail": "", "updated_at": "2026-09-18 09:00:00"},
    "last_refresh_at": {"value": "2026-09-18 10:00:00", "detail": "ci", "updated_at": ""},
}


class _FakeRepo:
    async def all_state(self, org: str, ns: str) -> dict[str, dict[str, Any]]:
        return dict(_STATE)

    async def table_counts(self, org: str, ns: str, **_: Any) -> dict[str, int]:
        return {"code_files": 3}


class _Counters:
    heads = 0
    counts: list[tuple[str, str]] = []
    ledger = 0


class _FakeGit:
    @staticmethod
    async def head_sha(git_dir: str, branch: str) -> str:
        _Counters.heads += 1
        return "c" * 40

    @staticmethod
    async def commit_count_between(git_dir: str, since: str, until: str) -> int:
        _Counters.counts.append((since, until))
        return 3 if since == "a" * 40 else 7


class _FakeCH:
    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "loom.worker_status" in sql:
            _Counters.ledger += 1
            return [
                {
                    "worker": "code_ingest",
                    "last_started": None,
                    "last_finished": None,
                    "last_error": "",
                    "last_status": "ok",
                    "consecutive_failures": 0,
                    "total_runs": 4,
                }
            ]
        return []


def _cfg(ns: str) -> Any:
    return repo_config(
        org="acme",
        namespace=ns,
        repo=f"acme/{ns}",
        git_dir=f"var/{ns}.git",
        git_branch="main",
        path_prefixes=(),
        org_readable=True,
    )


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    index_status.reset_repo_status_cache()
    _Counters.heads, _Counters.counts, _Counters.ledger = 0, [], 0
    monkeypatch.setattr(index_status, "CodeRepo", _FakeRepo)
    import server.code.app.index.gitrepo as gitrepo
    import server.core.ch as ch_mod

    monkeypatch.setattr(gitrepo, "head_sha", _FakeGit.head_sha)
    monkeypatch.setattr(gitrepo, "commit_count_between", _FakeGit.commit_count_between)
    monkeypatch.setattr(ch_mod, "get_client", lambda: _FakeCH())
    yield
    index_status.reset_repo_status_cache()


@pytest.mark.asyncio
async def test_the_worker_ledger_is_read_once_for_a_whole_listing() -> None:
    for i in range(5):
        await index_status._repo_status(_cfg(f"code-{i}"))
    assert _Counters.ledger == 1, "one ledger read per process per 15 s, not one per repository"


@pytest.mark.asyncio
async def test_lag_counts_are_remembered_per_sha_pair() -> None:
    st1 = await index_status._repo_status(_cfg("code-x"))
    assert (st1.fast_tier_lag, st1.scip_lag) == (3, 7)
    first = list(_Counters.counts)
    assert len(first) == 2  # two watermarks, both counted once, together
    st2 = await index_status._repo_status(_cfg("code-x"))
    assert (st2.fast_tier_lag, st2.scip_lag) == (3, 7)
    assert _Counters.counts == first, "the same (since, until) pair is not counted again"
    assert _Counters.heads == 2, "the clone tip is still read every time — it moves"


@pytest.mark.asyncio
async def test_a_failed_count_is_unknown_and_not_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.app.index.gitrepo as gitrepo

    calls = {"n": 0}

    async def _boom(git_dir: str, since: str, until: str) -> int:
        calls["n"] += 1
        raise RuntimeError("not an ancestor")

    monkeypatch.setattr(gitrepo, "commit_count_between", _boom)
    st = await index_status._repo_status(_cfg("code-y"))
    assert st.fast_tier_lag == -1 and st.scip_lag == -1
    await index_status._repo_status(_cfg("code-y"))
    assert calls["n"] == 4, "a failure is retried next time, only a number is remembered"


@pytest.mark.asyncio
async def test_index_status_serves_the_cached_status_on_a_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_status import CodeIndexStatusInput
    from server.settings import settings

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-z")
    monkeypatch.setattr(settings, "code_repo", "acme/code-z")
    monkeypatch.setattr(settings, "code_git_dir", "var/code-z.git")
    monkeypatch.setattr(settings, "code_git_branch", "main")
    monkeypatch.setattr(settings, "code_path_prefixes", "")
    monkeypatch.setattr(settings, "code_github_token", "")
    computed = {"n": 0}
    real = index_status._repo_status

    async def _counting(cfg: Any):
        computed["n"] += 1
        return await real(cfg)

    monkeypatch.setattr(index_status, "_repo_status", _counting)

    async def _none(org: str, ns: str, **_: Any) -> tuple[int, str]:
        return 0, ""

    monkeypatch.setattr(index_status, "symbol_stats_summary_cached", _none)
    monkeypatch.setattr(index_status, "unit_risk_summary_cached", _none)

    cfg = _cfg("code-z")
    listing_row = await index_status._repo_status_cached(cfg)  # what code.repos serves
    out1 = await index_status.handle_code_index_status(
        CodeIndexStatusInput(org="acme", namespace="code-z")
    )
    out2 = await index_status.handle_code_index_status(
        CodeIndexStatusInput(org="acme", namespace="code-z")
    )
    assert computed["n"] == 1, "index_status reads the 30 s status code.repos already holds"
    assert out1.ops and out1 is not listing_row and out2 is not out1
    assert not listing_row.ops, "the per-call fields were set on a copy, not on the cached row"
