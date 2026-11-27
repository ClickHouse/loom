from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from server.code.app.index import repo_registry
from server.code.domain.index import repos
from server.settings import settings


def test_a_public_remote_can_be_provisioned_without_a_jwt_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "")
    remote = "https://example.com/team/repo.git"
    assert repo_registry._stored_remote(remote) == (remote, "")


@pytest.mark.asyncio
async def test_provisioned_repository_keeps_its_settings_and_access_after_config_is_removed(
    monkeypatch,
) -> None:
    org, namespace = "o", "code-r"
    key = (org, namespace)
    config = repos.RepoConfig(
        org=org,
        namespace=namespace,
        repo="o/r",
        git_dir="cache/r.git",
        git_remote="https://git:credential-example@github.com/o/r",
        git_branch="main",
        path_prefixes=("",),
        github_token="private-fetch-token",
        org_readable=True,
        scip_checkout_dir="cache/checkout-r",
        scip_extra_producers=("go",),
        depends_on=("code-other",),
        git_fetch_pr_heads=False,
    )
    rows = {
        key: {
            "org": org,
            "namespace": namespace,
            "repo": config.repo,
            "classification": "restricted",
            "join_policy": "closed",
            "active": True,
            "row_kind": "access",
        }
    }

    class Store:
        def __init__(self, ch=None):
            pass

        async def registry_rows(self):
            return list(rows.values())

        async def code_repo_row(self, row_org, row_namespace):
            assert held == [(row_org, row_namespace)]
            return rows.get((row_org, row_namespace))

        async def insert_code_repo_row(self, row):
            assert held == [key]
            rows[key] = row

    held = []

    @asynccontextmanager
    async def claim(row_org, row_namespace):
        held.append((row_org, row_namespace))
        try:
            yield
        finally:
            held.pop()

    monkeypatch.setattr(settings, "jwt_secret", "unit")
    monkeypatch.setattr("server.auth.grant_claims.account_change", claim)
    monkeypatch.setattr("server.code.store.index.CodeRepo", Store)
    monkeypatch.setattr(repos, "_configured_from_env", lambda *_: [config])
    for field in (
        "_registry_repos",
        "_registry_namespaces",
        "_page_namespaces",
        "_registry_loaded",
    ):
        monkeypatch.setattr(repos, field, getattr(repos, field))
    monkeypatch.setattr(repo_registry, "_LOADED_AT", repo_registry._LOADED_AT)

    assert await repo_registry.read_registry()
    assert rows[key]["row_kind"] == "access"
    assert config.github_token not in str(rows[key])
    assert "credential-example" not in str(rows[key])
    monkeypatch.setattr(repos, "_configured_from_env", lambda *_: [])
    assert await repo_registry.read_registry()
    (registered,) = repos.configured_repos()
    assert registered.git_dir == config.git_dir
    assert registered.git_remote == config.git_remote
    assert registered.scip_checkout_dir == config.scip_checkout_dir
    assert registered.scip_extra_producers == ("go",)
    assert registered.path_prefixes == ("",)
    assert registered.depends_on == ("code-other",)
    assert not registered.git_fetch_pr_heads
    assert registered.github_token == config.github_token
    assert registered.classification == "restricted"
    assert not registered.org_readable

    rows[key] |= {"active": False, "row_kind": "page"}
    monkeypatch.setattr(repos, "_configured_from_env", lambda *_: [config])
    assert await repo_registry.read_registry()
    assert repos.configured_repos() == []
