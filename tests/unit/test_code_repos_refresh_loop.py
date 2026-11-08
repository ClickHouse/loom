"""Every process re-reads loom.code_repos on a timer, so a restriction set on one
replica reaches a replica that serves none of the ops that refresh it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import repo_registry
from server.code.domain.index.repos import registry_repos
from server.settings import settings


class _Repo:
    rows: list[dict[str, Any]] = []

    def __init__(self, ch: Any = None) -> None:
        pass

    async def registry_rows(self) -> list[dict[str, Any]]:
        return [dict(r, active=True) for r in self.rows]


def _row(classification: str) -> dict[str, Any]:
    return {
        "org": "acme",
        "namespace": "code-widgets",
        "repo": "acme/widgets",
        "branch": "main",
        "classification": classification,
    }


def test_a_new_restriction_reaches_an_idle_replica(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.code.store.index as store

    monkeypatch.setattr(store, "CodeRepo", _Repo)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(settings, "code_repos_refresh_seconds", 1)
    repo_registry.reset_for_tests()
    _Repo.rows = [_row("standard")]

    async def _go() -> list[str]:
        task = await repo_registry.start_refresher()
        seen = [c.classification for c in registry_repos()]
        _Repo.rows = [_row("restricted")]  # set_access on another replica
        await asyncio.sleep(1.3)  # no op on this one refreshes it
        seen += [c.classification for c in registry_repos()]
        task.cancel()
        return seen

    try:
        assert asyncio.run(_go()) == ["standard", "restricted"]
    finally:
        repo_registry.reset_for_tests()


def test_the_registry_counts_as_read_only_after_a_read_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.store.index as store

    class _Down(_Repo):
        async def registry_rows(self) -> list[dict[str, Any]]:
            raise ConnectionError("store unreachable")

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    repo_registry.reset_for_tests()
    try:
        monkeypatch.setattr(store, "CodeRepo", _Down)
        asyncio.run(repo_registry.refresh_db_repos(force=True))
        assert not repo_registry.registry_read()
        monkeypatch.setattr(store, "CodeRepo", _Repo)
        _Repo.rows = [_row("standard")]
        asyncio.run(repo_registry.refresh_db_repos(force=True))
        assert repo_registry.registry_read()
        # Self-service off: there is no registry to read.
        repo_registry.reset_for_tests()
        monkeypatch.setattr(settings, "code_self_service_enabled", False)
        assert repo_registry.registry_read()
    finally:
        repo_registry.reset_for_tests()


def test_a_failed_read_says_so_after_an_earlier_one_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.store.index as store

    class _Down(_Repo):
        async def registry_rows(self) -> list[dict[str, Any]]:
            raise ConnectionError("store unreachable")

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    repo_registry.reset_for_tests()
    try:
        monkeypatch.setattr(store, "CodeRepo", _Repo)
        _Repo.rows = [_row("standard")]
        assert asyncio.run(repo_registry.read_registry())
        monkeypatch.setattr(store, "CodeRepo", _Down)
        assert not asyncio.run(repo_registry.read_registry())
        assert repo_registry.registry_read()  # the last snapshot is kept
        assert [c.namespace for c in registry_repos()] == ["code-widgets"]
    finally:
        repo_registry.reset_for_tests()


def test_the_snapshot_keeps_the_namespace_of_a_removed_page_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.store.index as store
    from server.code.domain.index import repos

    class _Removed(_Repo):
        async def registry_rows(self) -> list[dict[str, Any]]:
            return [_row("standard") | {"active": False, "row_kind": "page"}]

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    repo_registry.reset_for_tests()
    try:
        monkeypatch.setattr(store, "CodeRepo", _Removed)
        assert asyncio.run(repo_registry.read_registry())
        assert repos.registry_repos() == []
        assert repos.page_namespaces() == {("acme", "code-widgets")}
    finally:
        repo_registry.reset_for_tests()
