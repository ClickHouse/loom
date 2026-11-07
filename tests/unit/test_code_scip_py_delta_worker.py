"""The Python compiler-tier worker — only `python`
producers, watermark `scip_commit_py`, a first run is a full index, the
checkout moves to git_head from the LOCAL bare clone, the harness's JSON
delta is applied in-process, a missing checkout is skipped (no install
step: scip-python reads the tree)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import scip_delta  # noqa: F401 (bound before CodeRepo is patched)
from server.code.workers import scip_py_delta as w
from tests.unit.code_fixtures import repo_config


def _Cfg(**kw: Any) -> Any:
    return repo_config(**{"org": "org", "namespace": "code-py", "scip_producer": "python", **kw})


def _Ts() -> Any:
    return _Cfg(namespace="code-ts", scip_producer="typescript")


def _repo(git_head: str, scip_commit_py: str) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return {"git_head": git_head, "scip_commit_py": scip_commit_py}.get(key, "")

    repo.get_state.side_effect = get_state
    repo.live_scip_paths.return_value = set()
    return repo


def _patched(repo, cfgs):
    return (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch("server.code.domain.index.repos.configured_repos", return_value=cfgs),
    )


@pytest.mark.asyncio
async def test_only_python_producers_are_checked_and_a_current_tier_is_a_noop():
    repo = _repo("a" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Ts(), _Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.repos_checked == 1 and res.deltas_applied == 0
    assert not res.errors and not res.skipped


@pytest.mark.asyncio
async def test_missing_checkout_is_created(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(tmp_path / "nope"))
    monkeypatch.setattr(w.settings, "code_git_dir", str(tmp_path))
    repo = _repo("a" * 40, "")
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert (tmp_path / "nope" / ".git").is_dir()
    assert not any("checkout missing" in s for s in res.skipped)


@pytest.mark.asyncio
async def test_missing_binary_skips(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    bare = tmp_path / "repo.git"
    bare.mkdir()
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))
    monkeypatch.setattr(w.settings, "code_scip_python_path", str(tmp_path / "no-scip-python"))
    repo = _repo("a" * 40, "")
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.skipped and "scip-python missing" in res.skipped[0]
    assert not res.errors


@pytest.mark.asyncio
async def test_first_run_is_full_and_the_delta_is_applied(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()  # no node_modules needed: scip-python reads the tree
    bare = tmp_path / "repo.git"
    bare.mkdir()
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))
    monkeypatch.setattr(w.settings, "code_scip_python_path", sys.executable)
    git_calls: list[tuple[str, ...]] = []

    async def fake_git(cwd, *args, **kw):
        git_calls.append(args)

    harness_calls: list[tuple[str, str]] = []

    async def fake_harness(co, base, head, delta_path):
        harness_calls.append((base, head))
        delta_path.write_text(
            json.dumps(
                {
                    "delta_version": 1,
                    "commit": head,
                    "files": [
                        {
                            "path": "pkg/a.py",
                            "symbols": [{"qualified_name": "pkg.a::f", "kind": "function"}],
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
    assert harness_calls == [("--full", "b" * 40)]
    assert git_calls[0][:2] == ("fetch", "--recurse-submodules=no") and str(bare) in git_calls[0]
    assert git_calls[1] == ("checkout", "--force", "b" * 40)
    assert applied == ["b" * 40]
    repo.set_state.assert_awaited_with("org", "code-py", "scip_commit_py", "b" * 40)


@pytest.mark.asyncio
async def test_no_python_change_in_window_advances_the_watermark(monkeypatch, tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    bare = tmp_path / "repo.git"
    bare.mkdir()
    monkeypatch.setattr(w.settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(w.settings, "code_git_dir", str(bare))
    monkeypatch.setattr(w.settings, "code_scip_python_path", sys.executable)

    async def fake_git(cwd, *args, **kw):
        return None

    async def fake_harness(co, base, head, delta_path):
        assert base == "a" * 40  # a seeded watermark: the window, not a full index

    monkeypatch.setattr(w, "git", fake_git)
    monkeypatch.setattr(w, "_run_harness", fake_harness)
    repo = _repo("b" * 40, "a" * 40)
    p1, p2 = _patched(repo, [_Cfg()])
    with p1, p2:
        res = await w.run_pass()
    assert res.deltas_applied == 0 and not res.errors
    repo.set_state.assert_awaited_with("org", "code-py", "scip_commit_py", "b" * 40)


def test_harness_env_and_registry(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(w.settings, "code_scip_python_path", str(tmp_path / "bin" / "scip-python"))
    env = w._harness_env(tmp_path / "delta.json")
    assert env["SCIP_PYTHON"].endswith("/bin/scip-python") and env["PATH"].startswith(
        str(tmp_path / "bin")
    )
    assert env["DELTA_OUT"].endswith("delta.json") and env["SCIP_PYTHON_HEAP_MB"] == "16384"
    assert w.WATERMARK == "scip_commit_py" and w.PRODUCER == "python"
    from server.code.workers.scip_apply import watermark_key_for

    assert watermark_key_for("scip-python") == w.WATERMARK, (
        "the apply worker and this one share the key"
    )
