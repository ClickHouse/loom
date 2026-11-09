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
    # Whether a revoke on a never-read process must read the registry and fan out.
    "code/app/index/maintainers.py",
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


def test_the_doctor_reads_the_registry_before_its_mirror_checks(monkeypatch) -> None:
    # The mirror checks list the configured entries, which refuse until that read.
    from server.cli import doctor
    from server.code.app.index import mirrors

    order: list[str] = []
    monkeypatch.setattr(doctor, "_read_code_registry", lambda: order.append("registry"))
    monkeypatch.setattr(mirrors, "doctor_checks", lambda: order.append("mirrors") or [])
    doctor._code_mirror_checks()
    assert order == ["registry", "mirrors"]


@pytest.mark.asyncio
async def test_memory_for_file_answers_without_links_while_the_registry_is_unread(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from server.memory.app import for_file

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    call = SimpleNamespace(org="o", extra_namespaces=["code-x"], tags=["repo:o/x"])
    assert await for_file._linked(call, "src/a.py", ["n"]) is None


@pytest.mark.asyncio
async def test_a_scoped_search_and_tracker_bindings_answer_while_the_registry_is_unread(
    monkeypatch,
) -> None:
    from server.code.app import repo_scope
    from server.tracker.app import binding

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    assert await repo_scope.readable_repo_namespace("o", "code-x") == ""
    assert binding.operator_repositories() == frozenset()


@pytest.mark.asyncio
async def test_a_service_account_bound_to_repositories_is_not_minted_while_unread(
    monkeypatch,
) -> None:
    # Its bound tracker grants come from the operator repositories, not known yet.
    from server.identity.app import me_service_accounts

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    from server.identity.app import namespace_kinds

    async def kinds(org: str) -> dict[str, str]:
        # code-x is a branch added from the page: its directory row says code
        return {"code-x": "code", "team-kb": "memory"}

    monkeypatch.setattr(namespace_kinds, "kind_map", kinds)
    with pytest.raises(repos.RegistryUnread):
        await me_service_accounts._bound_trackers("o", "s", object(), ["code-x"])  # type: ignore[arg-type]
    assert await me_service_accounts._bound_trackers("o", "s", object(), []) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_link_recheck_reads_nothing_while_the_registry_is_unread(monkeypatch) -> None:
    from server.memory.app import code_link_pass, code_link_recheck

    async def no_read() -> list[tuple[str, str]]:
        raise AssertionError("no repository is read")

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    monkeypatch.setattr(code_link_recheck.store, "code_namespaces", no_read)
    await code_link_recheck.recheck_all(code_link_pass.PassResult())
