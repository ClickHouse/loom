"""Tracker links are read per (org, code namespace). Code namespace names are
derived from the repository and branch, so two orgs can hold the same name: a
maintainer's link in one org never applies to the other, and the operator's map
applies only to the repositories the operator configured."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.code.domain.index import repos
from server.code.store.index import CodeRepo
from server.settings import settings
from server.tracker.app import binding

ORG_X, ORG_Y = "org-x", "org-y"
CODE = "code-widgets"


@pytest.fixture(autouse=True)
def _clean():
    binding.reset_overrides_for_tests()
    yield
    binding.reset_overrides_for_tests()


def test_a_maintainer_link_applies_only_in_its_own_org() -> None:
    binding.reset_overrides_for_tests({(ORG_Y, CODE): "tracker-shared"})
    assert binding.tracker_namespaces_for(ORG_Y, CODE) == ["tracker-shared"]
    assert binding.tracker_namespaces_for(ORG_X, CODE) == []
    assert binding.code_namespaces_linking(ORG_X, "tracker-shared", [CODE]) == []


def test_a_cleared_link_in_one_org_keeps_the_other_orgs_link() -> None:
    binding.reset_overrides_for_tests({(ORG_X, CODE): "tracker-x", (ORG_Y, CODE): ""})
    assert binding.tracker_namespaces_for(ORG_X, CODE) == ["tracker-x"]
    assert binding.tracker_namespaces_for(ORG_Y, CODE) == []


class _CH:
    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "org": ORG_X,
                "namespace": CODE,
                "state_key": "qa_binding",
                "state_value": "tracker-x",
            },
            {
                "org": ORG_Y,
                "namespace": CODE,
                "state_key": "qa_binding",
                "state_value": "tracker-y",
            },
        ]


def test_the_stored_links_keep_their_org() -> None:
    got = asyncio.run(CodeRepo(_CH()).state_values_for_keys([binding.OVERRIDE_KEY]))  # type: ignore[arg-type]
    assert got == {binding.OVERRIDE_KEY: {(ORG_X, CODE): "tracker-x", (ORG_Y, CODE): "tracker-y"}}


def test_the_operator_map_links_only_the_operator_s_repositories(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    added = repos.RepoConfig(
        org=ORG_Y,
        namespace="code-widgets-fork",
        repo="Other/widgets",
        git_dir="",
        git_remote="",
        git_branch="main",
        path_prefixes=(),
        github_token="",
        source="registry",
    )
    monkeypatch.setattr(repos, "_registry_repos", [added])
    assert binding.tracker_namespaces_for(ORG_X, CODE) == ["tracker-widgets"]
    # a self-service repository whose name matches the glob gets nothing from it
    assert binding.tracker_namespaces_for(ORG_Y, "code-widgets-fork") == []
    # nor does a namespace of the same name in an org the operator configured no entry for
    assert binding.tracker_namespaces_for(ORG_Y, CODE) == []


def test_an_operator_entry_with_a_maintainer_access_row_keeps_its_glob_link(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    access = repos.RepoConfig(
        org=ORG_X,
        namespace=CODE,
        repo="Acme/widgets",
        git_dir="",
        git_remote="",
        git_branch="main",
        path_prefixes=(),
        github_token="",
        classification="restricted",
        source="registry",
    )
    monkeypatch.setattr(repos, "_registry_repos", [access])
    (entry,) = repos.configured_repos()
    assert (entry.source, entry.access_source) == ("config", "registry")
    assert binding.tracker_namespaces_for(ORG_X, CODE) == ["tracker-widgets"]


def test_a_branch_follows_only_an_operator_source_and_its_own_row_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    branch = "code-widgets-26-8"
    monkeypatch.setattr(
        repos, "_registry_repos", [_entry(ORG_X, branch), _entry(ORG_X, "code-widgets-fork-1")]
    )
    binding.reset_overrides_for_tests(
        branches={(ORG_X, branch): CODE, (ORG_X, "code-widgets-fork-1"): "code-widgets-fork"}
    )
    assert binding.tracker_namespaces_for(ORG_X, branch) == ["tracker-widgets"]
    # a source that is not an operator entry is not followed
    assert binding.tracker_namespaces_for(ORG_X, "code-widgets-fork-1") == []
    # the row names a source in its own org only
    assert binding.tracker_namespaces_for(ORG_Y, branch) == []
    binding.reset_overrides_for_tests({(ORG_X, branch): "tracker-own"}, {(ORG_X, branch): CODE})
    assert binding.tracker_namespaces_for(ORG_X, branch) == ["tracker-own"]


def _entry(org: str, namespace: str) -> repos.RepoConfig:
    return repos.RepoConfig(
        org=org,
        namespace=namespace,
        repo="Acme/widgets",
        git_dir="",
        git_remote="",
        git_branch="26.8",
        path_prefixes=(),
        github_token="",
        source="registry",
    )


def test_a_branch_row_with_no_repository_entry_is_not_followed(monkeypatch) -> None:
    """A removal or a late copy can leave a `qa_branch_of` row behind; a namespace with
    no entry beside it reads nothing from the operator's repository."""
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    branch = "code-widgets-26-8"
    binding.reset_overrides_for_tests(branches={(ORG_X, branch): CODE})
    monkeypatch.setattr(repos, "_registry_repos", [])
    assert binding.tracker_namespaces_for(ORG_X, branch) == []
    monkeypatch.setattr(repos, "_registry_repos", [_entry(ORG_X, branch)])
    assert binding.tracker_namespaces_for(ORG_X, branch) == ["tracker-widgets"]
    # self-service switched off: the registry reads empty, and the row is not followed
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    assert binding.tracker_namespaces_for(ORG_X, branch) == []


def test_the_registry_namespaces_are_built_once_per_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_repos", [_entry(ORG_X, "code-widgets-26-8")])
    first = repos.registry_namespaces()
    assert first == {(ORG_X, "code-widgets-26-8")}
    assert repos.registry_namespaces() is first
    repos.set_registry_repos([_entry(ORG_Y, "code-widgets-26-8")])
    assert repos.registry_namespaces() == {(ORG_Y, "code-widgets-26-8")}
