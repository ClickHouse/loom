"""The TypeScript compiler-tier worker — only `typescript`
producers, watermark `scip_commit_ts`, a first run is a full index, the
checkout moves to git_head from the LOCAL bare clone, the harness's JSON
delta is applied in-process, and fresh checkouts prepare dependencies automatically."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.workers import scip_ts_delta as w
from tests.unit.code_fixtures import repo_config


@pytest.fixture(autouse=True)
def _scip_typescript_exists(monkeypatch) -> None:
    monkeypatch.setattr(w.settings, "code_scip_typescript_path", sys.executable)


def _Cfg(**kw: Any) -> Any:
    return repo_config(
        **{"org": "org", "namespace": "code-ts", "scip_producer": "typescript", **kw}
    )


def _Clang() -> Any:
    return _Cfg(namespace="code-cpp", scip_producer="clang")


def _repo(git_head: str, scip_commit_ts: str) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return {"git_head": git_head, "scip_commit_ts": scip_commit_ts}.get(key, "")

    repo.get_state.side_effect = get_state
    return repo


def _patched(repo, cfgs):
    return (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=cfgs),
    )


@pytest.mark.asyncio
async def test_only_typescript_producers_are_checked_and_a_current_tier_is_a_noop():
    repo = _repo("a" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Clang(), _Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.repos_checked == 1 and res.deltas_applied == 0
    assert not res.errors and not res.skipped


@pytest.mark.asyncio
async def test_unseeded_git_head_skips():
    repo = _repo("", "")
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.skipped and "git_head not seeded" in res.skipped[0]


@pytest.mark.asyncio
async def test_uninstalled_checkout_is_prepared_before_indexing(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    bare = tmp_path / "repo.git"
    bare.mkdir()
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))
    steps = []

    async def fake_git(cwd, *args, **kw):
        steps.append(args[0])

    async def prepare(co, projects, env, **kw):
        assert co == checkout and not (co / "node_modules").exists()
        steps.append("dependencies")

    async def harness(*a, **kw):
        steps.append("index")

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "prepare_dependencies", prepare)
    monkeypatch.setattr(w, "_run_harness", harness)
    repo = _repo("a" * 40, "")
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert not res.skipped and not res.errors, res.errors
    assert steps == ["fetch", "checkout", "dependencies", "index"]


@pytest.mark.asyncio
async def test_first_run_is_full_and_the_delta_is_applied(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout"
    (checkout / "node_modules").mkdir(parents=True)
    bare = tmp_path / "repo.git"
    bare.mkdir()
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))
    git_calls: list[tuple[str, ...]] = []

    async def fake_git(cwd, *args, **kw):
        git_calls.append(args)

    harness_calls: list[tuple[str, str]] = []

    async def fake_harness(co, base, head, delta_path, projects=()):
        harness_calls.append((base, head))
        delta_path.write_text(
            json.dumps(
                {
                    "delta_version": 1,
                    "commit": head,
                    "files": [
                        {
                            "path": "web/src/a.ts",
                            "symbols": [{"qualified_name": "web.src.a::f", "kind": "function"}],
                            "edges": [],
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "_run_harness", fake_harness)
    applied: list[str] = []

    async def fake_apply(repo, org, ns, delta):
        applied.append(delta.commit)
        from server.code.app.index.scip_delta import ScipApplyStats

        return ScipApplyStats(files=1, symbols_new=1)

    monkeypatch.setattr("server.code.app.index.scip_delta.apply_delta", fake_apply)
    repo = _repo("b" * 40, "")
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.deltas_applied == 1 and not res.errors, res.errors
    assert harness_calls == [("--full", "b" * 40)]  # no watermark: the first index is full
    assert git_calls[0][:2] == ("fetch", "--recurse-submodules=no") and str(bare) in git_calls[0]
    assert git_calls[1] == ("checkout", "--force", "b" * 40)
    assert applied == ["b" * 40]
    repo.set_state.assert_awaited_with("org", "code-ts", "scip_commit_ts", "b" * 40)


@pytest.mark.asyncio
async def test_no_typescript_change_in_window_advances_the_watermark(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout"
    (checkout / "node_modules").mkdir(parents=True)
    bare = tmp_path / "repo.git"
    bare.mkdir()
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))

    async def fake_git(cwd, *args, **kw):
        return None

    async def fake_harness(co, base, head, delta_path, projects=()):
        assert base == "a" * 40  # a seeded watermark: the window, not a full index
        # no delta file: the harness saw no TypeScript change

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "_run_harness", fake_harness)
    repo = _repo("b" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.deltas_applied == 0 and not res.errors
    repo.set_state.assert_awaited_with("org", "code-ts", "scip_commit_ts", "b" * 40)


def test_registry_and_producer_field():
    from server.code.domain.index.repos import RepoConfig

    assert RepoConfig.__dataclass_fields__["scip_producer"].default == ""
    assert w.WATERMARK == "scip_commit_ts" and w.PRODUCER == "typescript"


@pytest.mark.asyncio
async def test_a_lockfile_change_in_the_window_is_named(monkeypatch, tmp_path: Path):
    """Changed lockfiles refresh dependencies before the compiler watermark moves."""
    checkout = tmp_path / "checkout"
    (checkout / "node_modules").mkdir(parents=True)
    bare = tmp_path / "repo.git"
    bare.mkdir()
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))

    async def fake_git(cwd, *args, **kw):
        return None

    async def fake_harness(co, base, head, delta_path, projects=()):
        return None  # no TypeScript change in the window

    asked: list[tuple[str, str]] = []

    async def fake_lockfile_changed(co, base, head):
        asked.append((base, head))
        return True

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "_run_harness", fake_harness)
    monkeypatch.setattr(w, "_lockfile_changed", fake_lockfile_changed)
    prepared = AsyncMock()
    monkeypatch.setattr(w, "prepare_dependencies", prepared)
    repo = _repo("b" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert asked == [("a" * 40, "b" * 40)]
    assert len(res.lockfile_changed) == 1 and "automatically" in res.lockfile_changed[0]
    assert prepared.await_args.kwargs["force"] is True
    assert not res.errors
    repo.set_state.assert_awaited_with("org", "code-ts", "scip_commit_ts", "b" * 40)


def test_lockfile_names_cover_the_four_package_managers():
    assert {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb"} <= set(w._LOCKFILES)


@pytest.mark.asyncio
async def test_dependency_failure_does_not_run_indexer_or_advance_watermark(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "_resolve_paths", lambda cfg: (tmp_path, tmp_path, None))
    monkeypatch.setattr(w, "git", AsyncMock())
    monkeypatch.setattr(w, "_lockfile_changed", AsyncMock(return_value=False))
    monkeypatch.setattr(
        w, "prepare_dependencies", AsyncMock(side_effect=RuntimeError("install failed"))
    )
    harness = AsyncMock()
    monkeypatch.setattr(w, "_run_harness", harness)
    repo = _repo("b" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        result = await w.run_pass()
    assert result.errors and "install failed" in result.errors[0]
    harness.assert_not_awaited()
    # the only write is the failure's health record, never the watermark
    assert [c.args[2] for c in repo.set_state.await_args_list] == ["tier_health:typescript"]
