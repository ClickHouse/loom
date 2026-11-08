"""Before the self-service registry's first read, which operator entries a namespace
added from the page skips is not known: `configured_repos()` refuses, and only the
modules that decide access read the configuration through `config_entries()`, where no
operator entry is org-readable yet. A reader that goes round either fails here."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from server.code.domain.index import repos
from server.settings import settings

SERVER = Path(__file__).resolve().parents[2] / "server"

# What decides access: a namespace's kind and classification, whether it is a code entry.
CONFIG_ENTRY_READERS = {
    "identity/app/directory.py",
    "identity/app/namespace_classification.py",
    "identity/app/namespace_kinds.py",
    "identity/app/token_grants.py",
    "transport/namespace_resolve.py",
}
# The snapshot's internals and the raw configuration: only repos.py reads them.
INTERNALS = {"_configured_from_env", "_with_registry", "_registry_repos", "_page_namespaces"}


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (str(p.relative_to(SERVER)), ast.parse(p.read_text(), str(p)))
        for p in sorted(SERVER.rglob("*.py"))
    ]


def _names(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name)
    return out


def test_only_the_access_deciding_modules_read_config_entries() -> None:
    readers = {
        path
        for path, tree in _modules()
        if path != "code/domain/index/repos.py"
        and {"config_entries", "config_entry"} & _names(tree)
    }
    assert readers <= CONFIG_ENTRY_READERS, sorted(readers - CONFIG_ENTRY_READERS)


def test_no_module_reads_the_snapshot_or_the_raw_configuration_around_the_accessors() -> None:
    bypass = {
        path: sorted(INTERNALS & _names(tree))
        for path, tree in _modules()
        if path != "code/domain/index/repos.py" and INTERNALS & _names(tree)
    }
    assert bypass == {}
    # The operator's repositories and the scalar clone, read raw, skip nothing.
    raw = {
        path
        for path, tree in _modules()
        if {"code_repos_json", "code_git_dir", "_scalar_repo"} & _names(tree)
        and not path.startswith(("settings/", "code/domain/index/repos.py"))
    }
    assert raw == set()


def test_the_accessors_refuse_until_the_registry_is_read(monkeypatch) -> None:
    entry = repos.RepoConfig(
        org="o",
        namespace="code-x",
        repo="o/x",
        git_dir="",
        git_remote="",
        git_branch="main",
        path_prefixes=(),
        github_token="",
        org_readable=True,
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_configured_from_env", lambda: [entry])
    monkeypatch.setattr(repos, "registry_repos", lambda: [])
    monkeypatch.setattr(repos, "_registry_loaded", False)
    with pytest.raises(repos.RegistryUnread):
        repos.configured_repos()
    with pytest.raises(repos.RegistryUnread):
        repos.repo_for_namespace("o", "code-x")
    (listed,) = repos.config_entries()
    assert listed.namespace == "code-x" and listed.org_readable is False
    monkeypatch.setattr(repos, "_registry_loaded", True)
    assert repos.configured_repos() == [entry]
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    assert repos.configured_repos() == [entry]  # no registry to wait for


@pytest.mark.asyncio
async def test_a_memory_read_answers_without_links_while_the_registry_is_unread(
    monkeypatch,
) -> None:
    from server.memory.app import code_link_reads

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    memory = {"namespace": "n", "mid": "m", "tags": []}
    assert await code_link_reads.links_of("o", memory) == []


@pytest.mark.asyncio
async def test_the_mirrors_health_block_names_an_unread_registry(monkeypatch) -> None:
    from server import health

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    monkeypatch.setattr("server.code.app.index.mirrors.history_enabled", lambda: True)
    assert await health._mirrors_block() == {"registry": "unread"}
