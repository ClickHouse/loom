"""`code.repos` with `repo=owner/name` answers with that
repository's entries only, filtered before the status prefetch, and the key
is the same owner/name reduction the plugin's shunt applies to the origin
remote."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.app import index_status
from server.code.app import repos as repos_mod
from server.code.app.repos import repo_slug
from server.operations.base import Role
from tests.unit.code_fixtures import repo_config

ORG = "clickhouse"


def _cfg(ns: str, repo: str):
    return repo_config(
        org=ORG,
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
        _cfg("code-example-private", "ClickHouse/example-private"),
        _cfg("code-example-private-26-9", "ClickHouse/example-private"),
        _cfg("code-loom", "ClickHouse/loom"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    monkeypatch.setattr(index_status, "require_code_intel", lambda: None)

    async def _refresh():
        return None

    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", _refresh)
    warmed: list[str] = []

    async def _status(cfg):
        warmed.append(cfg.namespace)
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

    async def _allow(auth, role, org=None, namespace=None, **_kw):
        return None

    monkeypatch.setattr(authz, "check_authorized", _allow)

    async def _state(auth):
        return {}, {}, {}, None

    monkeypatch.setattr(repos_mod, "_repo_directory_state", _state)
    nc.reset()
    set_current_auth(
        AuthInfo(
            subject="sa:x",
            org=ORG,
            namespaces=("jdoe",),
            roles=(Role.VIEWER,),
            is_service_account=True,
        )
    )
    yield warmed
    nc.reset()
    set_current_auth(None)


def test_repo_slug_matches_the_shunts_reduction() -> None:
    for raw in (
        "ClickHouse/example-private",
        "git@github.com:ClickHouse/example-private.git",
        "https://github.com/ClickHouse/example-private.git",
        "https://github.com/ClickHouse/example-private/",
        "ssh://git@github.com/ClickHouse/example-private",
    ):
        assert repo_slug(raw) == "clickhouse/example-private", raw
    assert repo_slug("") == ""
    assert repo_slug("loom") == ""
    assert repo_slug(None) == ""  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_repo_filter_returns_that_repository_only_and_warms_only_it(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    out = await repos_mod.handle_code_repos(
        CodeReposInput(org=ORG, repo="clickhouse/example-private")
    )
    assert [r.namespace for r in out.repos] == [
        "code-example-private",
        "code-example-private-26-9",
    ]
    assert sorted(plane) == ["code-example-private", "code-example-private-26-9"]


@pytest.mark.asyncio
async def test_repo_filter_accepts_the_origin_url_form_and_an_unknown_repo_is_empty(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    out = await repos_mod.handle_code_repos(
        CodeReposInput(org=ORG, repo="git@github.com:ClickHouse/example-private.git")
    )
    assert len(out.repos) == 2
    index_status.reset_repo_status_cache()
    plane.clear()
    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG, repo="someone/else"))
    assert out.repos == [] and plane == []


@pytest.mark.asyncio
async def test_no_repo_keeps_the_full_listing(plane) -> None:
    from server.code.api.models_repos import CodeReposInput

    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    assert len(out.repos) == 4


@pytest.mark.asyncio
async def test_a_field_list_without_tier_health_still_gets_it(plane) -> None:
    """The dashboard does not name `tier_health`: a replica of the build before it
    would refuse the field during a rolling upgrade. The server sends it anyway."""
    from server.code.api.models_repos import CodeReposInput

    out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG, fields=["org"]))
    row = out.model_dump()["repos"][0]
    assert set(row) == {"org", "namespace", "tier_health"}
