"""`loom code backfill --commit <sha>` pins the indexed tree.

Paired chunking arms are indexed hours apart into sibling namespaces while
the shared bare mirror is fetched by the ingest worker in between; without a
pin each arm would read a different `refs/heads/master`. The pin resolves the
sha in the clone, never fetches, and refuses a rev the clone does not hold.
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

from server.cli import code, code_backfill
from server.code.app.index import gitrepo
from tests.unit.code_fixtures import repo_config

_SHA = "a1b53e235b973c27aa203c8bba421695b3098856"


def _args(**kw: object) -> argparse.Namespace:
    base = {"commit": "", "no_fetch": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_backfill_parser_accepts_commit() -> None:
    parser = argparse.ArgumentParser()
    code.register_code_cli(parser.add_subparsers(dest="cmd"))
    ns = parser.parse_args(["code", "backfill", "--embed", "--no-fetch", "--commit", _SHA[:12]])
    assert ns.commit == _SHA[:12]
    assert ns.no_fetch is True


def test_pinned_commit_is_resolved_and_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fetch(*a: object, **k: object) -> None:
        calls.append("fetch")

    async def _head(*a: object) -> str:
        calls.append("head")
        return "f" * 40

    async def _resolve(git_dir: str, rev: str) -> str:
        calls.append(f"resolve:{rev}")
        return _SHA

    monkeypatch.setattr(gitrepo, "fetch", _fetch)
    monkeypatch.setattr(gitrepo, "head_sha", _head)
    monkeypatch.setattr(gitrepo, "resolve_commit", _resolve)
    cfg = repo_config(git_remote="r", git_branch="master")
    got = asyncio.run(code_backfill._backfill_head("/g", cfg, _args(commit=_SHA[:12])))
    assert got == _SHA
    assert calls == [f"resolve:{_SHA[:12]}"]


def test_unpinned_backfill_keeps_fetching_the_tip(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fetch(*a: object, **k: object) -> None:
        calls.append("fetch")

    async def _head(*a: object) -> str:
        calls.append("head")
        return "f" * 40

    monkeypatch.setattr(gitrepo, "fetch", _fetch)
    monkeypatch.setattr(gitrepo, "head_sha", _head)
    cfg = repo_config(git_remote="r", git_branch="master")
    assert asyncio.run(code_backfill._backfill_head("/g", cfg, _args())) == "f" * 40
    assert calls == ["fetch", "head"]
    calls.clear()
    assert asyncio.run(code_backfill._backfill_head("/g", cfg, _args(no_fetch=True))) == "f" * 40
    assert calls == ["head"]


def test_resolve_commit_refuses_a_rev_the_clone_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _git(git_dir: str, *args: str, timeout_s: float = 0.0) -> str:
        raise RuntimeError("fatal: Needed a single revision")

    monkeypatch.setattr(gitrepo, "_git", _git)
    with pytest.raises(ValueError, match="not a commit"):
        asyncio.run(gitrepo.resolve_commit("/g", "deadbeef"))


def test_resolve_commit_returns_the_full_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _git(git_dir: str, *args: str, timeout_s: float = 0.0) -> str:
        assert args == ("rev-parse", "--verify", f"{_SHA[:12]}^{{commit}}")
        return _SHA + "\n"

    monkeypatch.setattr(gitrepo, "_git", _git)
    assert asyncio.run(gitrepo.resolve_commit("/g", _SHA[:12])) == _SHA
