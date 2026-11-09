"""The registry statements on ClickHouse: the snapshot read keeps a removed branch a
member added from the page, so an operator entry is never listed for its namespace,
and an operator entry's access row is not counted against the org's quota."""

from __future__ import annotations

import dataclasses
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
        reg.set_registry_repos([], frozenset())  # this test's rows are gone; still read
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
        entry,
        classification="restricted",
        join_policy="approval",
        created_by="o",
        current=await reg.live_row(entry),
    )
    assert await reg.read_registry()
    read = [c for c in repos.registry_repos() if c.org == org]
    assert {(c.namespace, c.row_kind) for c in read} == {
        ("code-live", "page"),
        ("code-entry", "access"),
    }
    listed = repos._with_registry([_entry(org, "code-removed"), _entry(org, "code-live"), entry])
    assert [(c.namespace, c.source) for c in listed if c.org == org] == [
        ("code-entry", "config"),
        ("code-live", "registry"),
    ]
    # The access row wins for its entry.
    (listed_entry,) = [c for c in listed if c.namespace == "code-entry"]
    assert (listed_entry.classification, listed_entry.access_source) == (
        "restricted",
        "registry",
    )
    assert await reg.active_count(org) == 1  # the access row is not counted


async def test_an_access_change_on_a_page_branch_keeps_every_column(org: str) -> None:
    await _add(org, "code-page")
    before = await CodeRepo().code_repo_row(org, "code-page")
    page = dataclasses.replace(_entry(org, "code-page"), source="registry")
    await reg.upsert_access(
        page,
        classification="restricted",
        join_policy="approval",
        created_by="admin",
        current=await reg.live_row(page),
    )
    after = await CodeRepo().code_repo_row(org, "code-page")
    assert before is not None and after is not None
    changed = {"classification", "join_policy", "created_by", "updated_at", "version"}
    assert {k: v for k, v in after.items() if k not in changed} == {
        k: v for k, v in before.items() if k not in changed
    }
    assert (after["row_kind"], after["classification"], after["created_by"]) == (
        "page",
        "restricted",
        "admin",
    )


async def test_the_registry_answers_only_once_read(
    org: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _add(org, "code-page")
    monkeypatch.setattr(repos, "_registry_loaded", False)
    with pytest.raises(repos.RegistryUnread):
        repos.configured_repos()
    # A failed read leaves it unread.
    with monkeypatch.context() as m:

        async def _down(self: CodeRepo) -> list[dict[str, Any]]:
            raise ConnectionError("down")

        m.setattr(CodeRepo, "registry_rows", _down)
        assert not await reg.read_registry()
    with pytest.raises(repos.RegistryUnread):
        repos.configured_repos()
    assert await reg.read_registry()
    assert (org, "code-page") in {(c.org, c.namespace) for c in repos.configured_repos()}


async def test_an_operator_entry_over_an_inactive_access_row_writes_a_fresh_one(org: str) -> None:
    # The entry's own token stays in the configuration, never in the access row.
    entry = dataclasses.replace(_entry(org, "code-entry"), github_token="entry-token")
    await reg.upsert_access(
        entry, classification="restricted", join_policy="approval", created_by="o", current=None
    )
    assert await CodeRepo().deactivate_code_repo(org, "code-entry")
    assert await reg.live_row(entry) is None
    await reg.upsert_access(
        entry, classification="standard", join_policy="open", created_by="o", current=None
    )
    row = await CodeRepo().code_repo_row(org, "code-entry")
    assert row is not None
    stamps = {"created_at", "updated_at", "version"}
    assert {k: v for k, v in row.items() if k not in stamps} == {
        "org": org,
        "namespace": "code-entry",
        "repo": "it/row-kind",
        "git_remote": "https://github.com/it/row-kind.git",
        "git_branch": "main",
        "path_prefixes": [],
        "exclude": [],
        "classification": "standard",
        "join_policy": "open",
        "github_token_enc": "",
        "created_by": "o",
        "active": True,
        "exclude_generated": False,
        "scip_producer": "",
        "row_kind": "access",
    }
