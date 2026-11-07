"""`code.repos` takes the checkout's remotes as ordered
`repos` candidates — the first one the caller reads wins — and a candidate
that is a GitHub fork resolves to its parent, then its source, through the
same per-entry gate (no new access path)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import server.auth.authz as authz
import server.code.app.index.fork_upstream as fu
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.app import index_status
from server.code.app import repos as repos_mod
from server.operations.base import Role
from server.settings import settings
from tests.unit.code_fixtures import repo_config

ORG = "clickhouse"


def _cfg(ns: str, repo: str, org: str = ORG):
    return repo_config(
        org=org,
        namespace=ns,
        repo=repo,
        git_branch="master",
        classification="standard",
        org_readable=True,
    )


@pytest.fixture
def plane(monkeypatch):
    import server.code.domain.index.repos as repos

    entries = [
        _cfg("code-clickhouse", "ClickHouse/ClickHouse"),
        _cfg("code-loom", "ClickHouse/loom"),
        _cfg("code-secret", "Other/secret", org="elsewhere"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    monkeypatch.setattr(index_status, "require_code_intel", lambda: None)

    async def _refresh():
        return None

    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", _refresh)

    async def _status(cfg):
        return SimpleNamespace(
            org=cfg.org,
            namespace=cfg.namespace,
            repo=cfg.repo,
            branch=cfg.git_branch,
            git_head="h",
            clone_head="h",
            fast_tier_lag=0,
            scip_lag=0,
            scip_py_lag=0,
            tier_health=[],
            state={},
            last_refresh_at="",
            counts={},
            chunks_total=0,
            chunks_embedded=0,
            embedding_coverage=0.0,
        )

    monkeypatch.setattr(index_status, "_repo_status", _status)
    index_status.reset_repo_status_cache()
    denied: set[str] = {"code-secret"}

    async def _gate(auth, role, org=None, namespace=None, **_kw):
        if namespace in denied:
            raise PermissionError(namespace)

    monkeypatch.setattr(authz, "check_authorized", _gate)

    async def _state(auth):
        return {}, {}, {}, None

    monkeypatch.setattr(repos_mod, "_repo_directory_state", _state)
    nc.reset()
    fu.reset()
    set_current_auth(
        AuthInfo(
            subject="sa:x",
            org=ORG,
            namespaces=("jdoe",),
            roles=(Role.VIEWER,),
            is_service_account=True,
        )
    )
    yield denied
    nc.reset()
    fu.reset()
    set_current_auth(None)


def _github(answers: dict[str, object], calls: list[str]):
    async def fetch(slug: str) -> list[str]:
        calls.append(slug)
        a = answers.get(slug, [])
        if isinstance(a, BaseException):
            raise a
        if a == "slow":
            await asyncio.sleep(5)
        return list(a)  # type: ignore[arg-type]

    return fetch


@pytest.fixture
def github(monkeypatch):
    answers: dict[str, object] = {}
    calls: list[str] = []
    monkeypatch.setattr(fu, "_github_upstreams", _github(answers, calls))
    return answers, calls


async def _repos(**kw):
    from server.code.api.models_repos import CodeReposInput

    return (await repos_mod.handle_code_repos(CodeReposInput(org=ORG, **kw))).repos


@pytest.mark.asyncio
async def test_first_candidate_with_a_readable_entry_wins(plane, github) -> None:
    rows = await _repos(
        repos=[
            "git@github.com:someone/ClickHouse.git",
            "https://github.com/ClickHouse/loom",
            "ClickHouse/ClickHouse",
        ]
    )
    assert [r.namespace for r in rows] == ["code-loom"]
    assert rows[0].matched_via == "candidate" and rows[0].checkout_slug == "clickhouse/loom"
    assert github[1] == []  # a candidate matched: GitHub is never asked


@pytest.mark.asyncio
async def test_an_unreadable_candidate_yields_to_the_next(plane, github) -> None:
    rows = await _repos(repos=["Other/secret", "ClickHouse/ClickHouse"])
    assert [r.namespace for r in rows] == ["code-clickhouse"]


@pytest.mark.asyncio
async def test_a_fork_resolves_to_its_parent_then_source(plane, github) -> None:
    answers, calls = github
    answers["someone/clickhouse"] = ["someone-else/ClickHouse", "ClickHouse/ClickHouse"]
    rows = await _repos(repos=["someone/ClickHouse"])
    assert [r.namespace for r in rows] == ["code-clickhouse"]
    assert rows[0].matched_via == "fork_of" and rows[0].checkout_slug == "someone/clickhouse"
    assert calls == ["someone/clickhouse"]
    # the cache answers the second call: no fetch
    rows = await _repos(repos=["someone/ClickHouse"])
    assert [r.namespace for r in rows] == ["code-clickhouse"] and calls == ["someone/clickhouse"]


@pytest.mark.asyncio
async def test_parent_wins_over_source(plane, github) -> None:
    answers, _ = github
    answers["me/loom-fork"] = ["ClickHouse/loom", "ClickHouse/ClickHouse"]
    rows = await _repos(repos=["me/loom-fork"])
    assert [r.namespace for r in rows] == ["code-loom"]


@pytest.mark.parametrize("answer", [[], RuntimeError("boom"), "slow"])
@pytest.mark.asyncio
async def test_non_fork_error_or_timeout_is_not_indexed(plane, github, monkeypatch, answer) -> None:
    answers, calls = github
    answers["someone/clickhouse"] = answer
    monkeypatch.setattr(settings, "code_fork_upstream_budget_s", 0.05)
    assert await _repos(repos=["someone/ClickHouse"]) == []
    assert await _repos(repos=["someone/ClickHouse"]) == []
    assert calls == ["someone/clickhouse"]  # the miss is cached too


@pytest.mark.asyncio
async def test_a_fork_of_an_unreadable_upstream_stays_omitted(plane, github) -> None:
    answers, _ = github
    answers["me/secret"] = ["Other/secret"]
    assert await _repos(repos=["me/secret"]) == []


@pytest.mark.asyncio
async def test_only_the_first_three_candidates_are_asked(plane, github) -> None:
    _, calls = github
    await _repos(repos=["a/1", "b/2", "c/3", "d/4"])
    assert calls == ["a/1", "b/2", "c/3"]


@pytest.mark.asyncio
async def test_repo_alone_is_unchanged(plane, github) -> None:
    answers, calls = github
    answers["someone/clickhouse"] = ["ClickHouse/ClickHouse"]
    assert await _repos(repo="someone/ClickHouse") == []
    assert calls == []  # no fork lookup for this form
    rows = await _repos(repo="ClickHouse/ClickHouse")
    assert [r.namespace for r in rows] == ["code-clickhouse"]
    assert rows[0].matched_via is None and rows[0].checkout_slug is None
    assert len(await _repos()) == 2


def test_repos_is_bounded() -> None:
    from pydantic import ValidationError

    from server.code.api.models_repos import CodeReposInput

    with pytest.raises(ValidationError):
        CodeReposInput(repos=[f"o/{i}" for i in range(9)])
    with pytest.raises(ValidationError):
        CodeReposInput(repos=["o/" + "x" * 300])


@pytest.mark.asyncio
async def test_github_client_reads_parent_and_source(monkeypatch) -> None:
    import server.clients.github as gh

    async def _get(url, token, params=None):
        assert url.endswith("/repos/someone/ClickHouse")
        return {
            "fork": True,
            "parent": {"full_name": "mid/ClickHouse"},
            "source": {"full_name": "ClickHouse/ClickHouse"},
        }

    monkeypatch.setattr(gh, "_get", _get)
    assert await gh.repo_fork_upstreams("someone/ClickHouse", "") == [
        "mid/ClickHouse",
        "ClickHouse/ClickHouse",
    ]

    async def _not_fork(url, token, params=None):
        return {"fork": False}

    monkeypatch.setattr(gh, "_get", _not_fork)
    assert await gh.repo_fork_upstreams("ClickHouse/ClickHouse", "") == []


def test_fork_lookup_never_sends_a_crafted_slug_to_github() -> None:
    """A candidate outside GitHub's owner/name grammar is dropped before the
    authenticated request (no URL steering with the server's token)."""
    import asyncio

    from server.code.app.index import fork_upstream as fu

    fu.reset()
    seen: list[str] = []

    async def fetch(slug: str) -> list[str]:
        seen.append(slug)
        return []

    bad = ["../..", "a/b?x=y", "a/..", "owner/na me", "-x/y", "a/b#c", ""]
    asyncio.run(fu.fork_upstreams(bad, fetch=fetch))
    assert seen == []
    assert fu.valid_slug("someone/clickhouse") and fu.valid_slug("clickhouse/deploy-app")
    asyncio.run(fu.fork_upstreams(["someone/clickhouse"], fetch=fetch))
    assert seen == ["someone/clickhouse"]


def test_fork_cache_is_bounded() -> None:
    from server.code.app.index import fork_upstream as fu

    fu.reset()
    for i in range(fu.MAX_CACHE + 50):
        fu._cache.put(f"o/r{i}", ())
    assert len(fu._cache) <= fu.MAX_CACHE


def test_a_cancelled_caller_takes_its_github_reads_with_it() -> None:
    fu.reset()
    started = asyncio.Event()
    cancelled: list[str] = []

    async def fetch(slug: str) -> list[str]:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(slug)
            raise
        return []

    async def main() -> None:
        caller = asyncio.ensure_future(
            fu.fork_upstreams(["someone/clickhouse"], fetch=fetch, budget_s=30)
        )
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.sleep(0)  # let the cancelled read unwind
        assert cancelled == ["someone/clickhouse"]

    asyncio.run(main())
