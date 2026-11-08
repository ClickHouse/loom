"""A configured entry is not read into Loom before its first index, and a restricted
entry's bare mirror is mode 0700 whenever it became restricted.

The runbook restricts a private repository between `loom code init` and `loom code
backfill`: until the backfill writes `git_head`, no worker writes the repository's
content into the still-standard namespace, and once it is restricted (by the entry or
by `code.repo.set_access`) the mirror cloned meanwhile is tightened on every host."""

from __future__ import annotations

import dataclasses
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.workers import pr_history as w
from tests.unit.code_fixtures import repo_config


def _repo(head: str) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org: str, ns: str, key: str) -> str:
        return head if key == "git_head" else ""

    repo.get_state.side_effect = get_state
    return repo


async def _pr_history_pass(cfg: Any, repo: AsyncMock) -> list[str]:
    called: list[str] = []

    async def _prs(*_a: Any, **_k: Any) -> Any:
        called.append("pulls")
        return []

    async def _items(*_a: Any, **_k: Any) -> Any:
        called.append("items")
        return []

    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=[cfg]),
        patch("server.clients.github.merged_prs_since", _prs),
        patch("server.clients.github.github_items_since", _items),
    ):
        await w.run_pass()
    return called


@pytest.mark.asyncio
async def test_pr_history_writes_nothing_for_a_configured_entry_before_its_first_index() -> None:
    repo = _repo("")
    cfg = repo_config(repo="Org/Private", github_token="tok")
    assert await _pr_history_pass(cfg, repo) == []
    repo.insert_rows.assert_not_called()
    repo.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_pr_history_syncs_an_indexed_entry_and_a_self_service_one() -> None:
    indexed = repo_config(repo="Org/Private", github_token="tok")
    assert "pulls" in await _pr_history_pass(indexed, _repo("a" * 40))
    added = repo_config(repo="Org/Added", github_token="tok", source="registry")
    assert "pulls" in await _pr_history_pass(added, _repo(""))


def _mirror(tmp_path: Any) -> str:
    git_dir = tmp_path / "r.git"
    git_dir.mkdir()
    os.chmod(git_dir, 0o755)
    return str(git_dir)


def _mode(path: str) -> int:
    return os.stat(path).st_mode & 0o777


class _Client:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.inserted: list[Any] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return dict(self.row) if self.row else None

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))


@pytest.mark.asyncio
async def test_every_ingest_pass_tightens_a_restricted_entrys_mirror(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not once per process: the entry may be restricted after this host first saw it."""
    from server.code.app.index import ingest_pass as ip
    from server.code.app.index import registry
    from server.code.app.index.pass_budget import CodeIngestResult
    from server.code.domain.index import repos

    git_dir = _mirror(tmp_path)
    standard = repo_config(git_dir=git_dir)
    monkeypatch.setattr(repos, "configured_repos", lambda: [standard])
    monkeypatch.setattr(registry, "ensure_registered", AsyncMock(return_value=False))
    monkeypatch.setattr(ip, "fetch_entry", AsyncMock(return_value=False))
    await ip._run_repo(standard, CodeIngestResult())
    assert _mode(git_dir) == 0o755
    # the config restricts it: the classification reads the configured entry
    restricted = repo_config(git_dir=git_dir, classification="restricted")
    monkeypatch.setattr(repos, "configured_repos", lambda: [restricted])
    await ip._run_repo(restricted, CodeIngestResult())
    assert _mode(git_dir) == 0o700
    os.chmod(git_dir, 0o755)
    await ip._run_repo(restricted, CodeIngestResult())
    assert _mode(git_dir) == 0o700


@pytest.mark.asyncio
async def test_set_access_restricted_tightens_the_mirror(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app import repo_admin as h
    from server.code.app.index import repo_registry as reg
    from server.identity.app import namespace_classification as nc

    git_dir = _mirror(tmp_path)
    checkout = tmp_path / "checkout-code-p"
    sibling = tmp_path / "checkout-code-p.go"
    for d in (checkout, sibling):
        d.mkdir()
        os.chmod(d, 0o755)
    cfg = repo_config(namespace="code-p", git_dir=git_dir, scip_checkout_dir=str(checkout))

    async def _noop(*_a: Any, **_k: Any) -> Any:
        return []

    monkeypatch.setattr(reg, "refresh_db_repos", _noop)
    monkeypatch.setattr(reg, "upsert_access", _noop)
    monkeypatch.setattr("server.code.domain.index.repos.repo_for_namespace", lambda org, ns: cfg)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client(None))
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", "o"))
    # this process already holds the org's snapshot: the restriction joins it at once
    monkeypatch.setitem(nc._cache, "o", (float("inf"), frozenset()))
    try:
        await h.handle_code_repo_set_access(
            SimpleNamespace(
                org="o",
                namespace="code-p",
                classification="restricted",
                join_policy="approval",
                confirm="",
            )
        )
        assert nc.is_restricted_cached("o", "code-p")
    finally:
        nc.reset()
    assert _mode(git_dir) == _mode(str(checkout)) == _mode(str(sibling)) == 0o700


@pytest.mark.asyncio
async def test_each_host_round_tightens_a_restricted_entrys_mirror(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.identity.app.namespace_classification as nc
    from server.code.app.index import mirrors
    from server.code.domain.index import repos

    restricted = _mirror(tmp_path)
    standard, by_directory = str(tmp_path / "s.git"), str(tmp_path / "d.git")
    for d in (standard, by_directory):
        os.mkdir(d)
        os.chmod(d, 0o755)
    cfgs = [
        repo_config(namespace="code-p", git_dir=restricted, classification="restricted"),
        repo_config(namespace="code-s", git_dir=standard),
        # set_access with self-service off: only the directory row says restricted
        repo_config(namespace="code-d", git_dir=by_directory),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: cfgs)
    monkeypatch.setitem(nc._cache, "o", (float("inf"), frozenset({"code-d"})))

    async def _none(*_a: Any, **_k: Any) -> Any:
        return []

    monkeypatch.setattr(mirrors, "_clone_candidates", _none)
    monkeypatch.setattr(mirrors, "_fetch_round", _none)
    monkeypatch.setattr(mirrors, "gc_if_due", _none)
    monkeypatch.setattr(mirrors, "sweep", _none)
    await mirrors.host_round()
    assert _mode(restricted) == _mode(by_directory) == 0o700 and _mode(standard) == 0o755


def test_a_host_without_the_mirror_has_nothing_to_tighten(tmp_path: Any) -> None:
    from server.code.app.index.bootstrap import restrict_dir

    absent = tmp_path / "absent.git"
    restrict_dir(str(absent))
    restrict_dir("")
    assert not absent.exists()  # nothing is created where there was nothing


def test_a_failed_chmod_is_logged_once_per_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from server.code.app.index import bootstrap

    git_dir = _mirror(tmp_path)

    def _refuse(path: Any, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(bootstrap, "_CHMOD_FAILED", set())
    monkeypatch.setattr(bootstrap.os, "chmod", _refuse)
    with caplog.at_level(logging.WARNING, logger="loom.code_index.bootstrap"):
        for _ in range(3):
            bootstrap.restrict_dir(git_dir)
    assert sum("could not set" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_an_unreadable_classification_skips_the_pass_and_changes_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tightening every entry on an outage could not be undone when it ends; the next
    pass, which reads the classification, tightens what is restricted."""
    from server.auth.directory import ClassificationUnavailable
    from server.code.app.index import bootstrap

    git_dir = _mirror(tmp_path)

    async def _down(org: str, ns: str) -> bool:
        raise ClassificationUnavailable("directory unreachable")

    monkeypatch.setattr("server.identity.app.namespace_classification.is_restricted", _down)
    await bootstrap.restrict_if_restricted(repo_config(git_dir=git_dir))
    assert _mode(git_dir) == 0o755


def test_every_compiler_checkout_is_created_0700(tmp_path: Any) -> None:
    from server.code.app.index.compiler_tier import resolve_checkout

    bare = _mirror(tmp_path)
    for classification, name in (("restricted", "co-r"), ("standard", "co-s")):
        cfg = repo_config(
            git_dir=bare,
            scip_checkout_dir=str(tmp_path / name),
            scip_producer="go",
            classification=classification,
        )
        checkout, _bare, missing = resolve_checkout(cfg, "go")
        assert missing is None and (checkout / ".git").is_dir()
        assert _mode(str(checkout)) == 0o700  # git init keeps the directory's mode


@pytest.mark.asyncio
async def test_a_restriction_joins_the_snapshot_and_a_widening_drops_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app import repo_admin as h
    from server.code.app.index import repo_registry as reg
    from server.identity.app import namespace_classification as nc

    entry = {"cfg": repo_config(namespace="code-p", git_dir=_mirror(tmp_path))}

    async def _noop(*_a: Any, **_k: Any) -> Any:
        return []

    monkeypatch.setattr(reg, "refresh_db_repos", _noop)
    monkeypatch.setattr(reg, "upsert_access", _noop)
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace", lambda org, ns: entry["cfg"]
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client(None))
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", "o"))
    monkeypatch.setitem(nc._cache, "o", (float("inf"), frozenset({"code-other"})))

    def _set(classification: str, join_policy: str) -> Any:
        return SimpleNamespace(
            org="o",
            namespace="code-p",
            classification=classification,
            join_policy=join_policy,
            confirm="code-p",
        )

    try:
        await h.handle_code_repo_set_access(_set("restricted", "approval"))
        assert nc._cache["o"][1] == {"code-other", "code-p"}
        # the entry now reads restricted: setting it standard widens it
        entry["cfg"] = dataclasses.replace(entry["cfg"], classification="restricted")
        await h.handle_code_repo_set_access(_set("standard", "approval"))
        assert "o" not in nc._cache
    finally:
        nc.reset()


@pytest.mark.asyncio
async def test_a_cloned_mirror_is_0700_whatever_mode_the_clone_left(tmp_path) -> None:
    from server.code.app.index import bootstrap, sandbox

    git_dir = str(tmp_path / "private.git")
    cfg = repo_config(
        repo="Org/Private", git_dir=git_dir, git_remote="https://github.com/Org/Private.git"
    )

    async def clone(argv: list[str], **_: Any) -> SimpleNamespace:
        target = argv[-1]
        os.chmod(target, 0o755)  # as a clone that resets the target's mode would
        return SimpleNamespace(returncode=0, stderr=b"")

    with patch.object(sandbox, "run_job", clone):
        assert await bootstrap.clone_mirror(cfg) == ""
    assert os.stat(git_dir).st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_a_mirror_loom_code_init_clones_is_0700(tmp_path, monkeypatch) -> None:
    from server.cli import code_maintenance as cm

    git_dir = str(tmp_path / "standard.git")
    cfg = repo_config(git_dir=git_dir, git_remote="https://github.com/Org/Repo.git")

    umasks: list[int] = []

    async def clone(*argv: str, **kw: Any) -> None:
        umasks.append(kw["umask"])  # git creates the mirror private from its first file
        os.makedirs(argv[-1], 0o755)

    class _Stop(Exception):
        pass

    async def stop() -> None:
        raise _Stop

    monkeypatch.setattr(cm, "_repo_cfg", lambda _args: cfg)
    monkeypatch.setattr(cm.asyncio, "create_subprocess_exec", clone)
    monkeypatch.setattr(cm, "wait_or_stop", AsyncMock(return_value=0))
    monkeypatch.setattr("server.core.ch.get_client", lambda: SimpleNamespace(connect=stop))
    with pytest.raises(_Stop):  # stops after the clone, before any ClickHouse write
        await cm._init(SimpleNamespace())
    assert umasks == [0o077] and _mode(git_dir) == 0o700
