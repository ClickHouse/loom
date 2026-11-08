"""The registry statements on ClickHouse: the snapshot read keeps a removed branch a
member added from the page, so an operator entry is never listed for its namespace,
and an operator entry's access row is no repository against the org's quota."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.code.app.index import repo_registry as reg
from server.code.domain.index import repos
from server.code.store.index import CodeRepo
from server.core.repo_base import _default_client
from server.settings import settings
from tests.integration._resources import unique_org


@pytest.fixture
async def org(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    client = _default_client()
    await client.connect()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    name = unique_org("row-kind")
    try:
        yield name
    finally:
        await client.execute(
            "DELETE FROM loom.code_repos WHERE org = {org:String}", params={"org": name}
        )
        reg.reset_for_tests()
        await client.disconnect()


def _entry(org: str, namespace: str) -> Any:
    return repos.RepoConfig(
        org=org,
        namespace=namespace,
        repo="it/row-kind",
        git_dir="",
        git_remote="",
        git_branch="main",
        path_prefixes=(),
        github_token="",
    )


async def _add(org: str, namespace: str) -> None:
    await reg.insert_entry(
        org=org,
        namespace=namespace,
        repo="it/row-kind",
        branch=namespace,
        classification="standard",
        join_policy="open",
        github_token="",
        path_prefixes=[],
        exclude=[],
        created_by="member",
    )


async def test_a_removed_page_branch_keeps_its_namespace_from_the_operator(org: str) -> None:
    await _add(org, "code-removed")
    assert await CodeRepo().deactivate_code_repo(org, "code-removed")
    await _add(org, "code-live")
    entry = _entry(org, "code-entry")
    await reg.upsert_access(
        entry, classification="restricted", join_policy="approval", created_by="o"
    )
    assert await reg.read_registry()
    mine = [c for c in repos.registry_repos() if c.org == org]
    assert {(c.namespace, c.row_kind) for c in mine} == {
        ("code-live", "page"),
        ("code-entry", "access"),
    }
    listed = repos._with_registry([_entry(org, "code-removed"), _entry(org, "code-live"), entry])
    assert [(c.namespace, c.source) for c in listed if c.org == org] == [
        ("code-entry", "config"),
        ("code-live", "registry"),
    ]
    assert await reg.active_count(org) == 1  # the access row is not counted
