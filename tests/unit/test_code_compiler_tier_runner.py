"""Every compiler tier runs the same pass: entries it does not own are not counted,
an entry without a checkout is skipped with the reason, a window whose base is gone
clears the watermark, and any other failure is noted on the pass."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import compiler_tier
from server.workers import pass_context

TIERS = [
    ("code_scip_delta", "server.code.workers.scip_delta", "scip_commit"),
    ("code_scip_ts_delta", "server.code.workers.scip_ts_delta", "scip_commit_ts"),
    ("code_scip_py_delta", "server.code.workers.scip_py_delta", "scip_commit_py"),
    ("code_scip_go_delta", "server.code.workers.scip_go_delta", "scip_commit_go"),
]


class _Cfg:
    org = "o"
    namespace = "code-x"
    git_branch = ""
    scip_producer = ""
    scip_extra_producers: tuple[str, ...] = ()
    scip_ts_projects: tuple[str, ...] = ()
    source = "config"

    def __init__(self, root: Path) -> None:
        self.git_dir = str(root / "repo.git")
        self.git_remote = str(root / "mirror.git")  # a local mirror
        self.scip_checkout_dir = str(root / "checkout")


def _repo(watermark: str) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org: str, ns: str, key: str) -> str:
        return {"git_head": "b" * 40, watermark: "a" * 40}.get(key, "")

    repo.get_state.side_effect = get_state
    return repo


async def _run(w: Any, cfg: Any, repo: Any, owns: bool) -> tuple[Any, pass_context.PassContext]:
    @asynccontextmanager
    async def _lock(*_: Any, **__: Any) -> Any:
        yield

    ctx = pass_context.begin("w")
    try:
        with (
            patch("server.code.store.index.CodeRepo", return_value=repo),
            patch("server.code.domain.index.repos.configured_repos", return_value=[cfg]),
            patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=owns)),
            patch.object(compiler_tier, "entry_lock", _lock),
        ):
            result = await w.run_pass()
    finally:
        pass_context.clear()
    return result, ctx


def _outcome(worker: str) -> dict[str, Any]:
    from server.workers.status import get_registry

    return get_registry().snapshot()[worker]["last_outcome"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("worker", "module", "watermark"), TIERS)
async def test_an_entry_the_tier_does_not_own_is_not_counted(
    worker: str, module: str, watermark: str, tmp_path: Path
) -> None:
    w = importlib.import_module(module)
    result, ctx = await _run(w, _Cfg(tmp_path), _repo(watermark), owns=False)
    assert (result.repos_checked, result.deltas_applied, result.skipped) == (0, 0, [])
    assert ctx.count == 0 and _outcome(worker)["reason"] == "current"


@pytest.mark.asyncio
async def test_an_operator_scip_clang_entry_without_its_build_tree_is_skipped(
    tmp_path: Path,
) -> None:
    worker, module, watermark = TIERS[0]
    w = importlib.import_module(module)
    (tmp_path / "repo.git").mkdir()
    (tmp_path / "a-file").write_text("")  # the checkout cannot be created under it
    cfg = _Cfg(tmp_path)
    cfg.scip_checkout_dir = str(tmp_path / "a-file" / "checkout")
    result, ctx = await _run(w, cfg, _repo(watermark), owns=True)
    assert result.repos_checked == 1 and not result.errors and ctx.count == 0
    assert len(result.skipped) == 1
    assert result.skipped[0].startswith("code-x: ") and "checkout missing at" in result.skipped[0]
    assert _outcome(worker)["reason"] == result.skipped[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("worker", "module", "watermark"), TIERS[1:])
async def test_an_entry_without_a_checkout_gets_an_empty_one(
    worker: str, module: str, watermark: str, tmp_path: Path
) -> None:
    w = importlib.import_module(module)
    (tmp_path / "repo.git").mkdir()
    result, ctx = await _run(w, _Cfg(tmp_path), _repo(watermark), owns=True)
    assert result.repos_checked == 1 and not result.errors and ctx.count == 0
    assert (tmp_path / f"checkout.{w.PRODUCER}" / ".git").is_dir()
    assert not any("checkout missing" in s for s in result.skipped)


@pytest.mark.asyncio
@pytest.mark.parametrize(("worker", "module", "watermark"), TIERS)
async def test_a_failing_entry_is_noted_and_named(
    worker: str, module: str, watermark: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = importlib.import_module(module)

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("repo down")

    monkeypatch.setattr(w, "_run_repo", boom)
    result, ctx = await _run(w, _Cfg(tmp_path), _repo(watermark), owns=True)
    assert result.errors == ["code-x: RuntimeError: repo down"]
    assert ctx.count == 1 and ctx.failures[0][:2] == ("code-x", "RuntimeError")
    assert _outcome(worker)["reason"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(("worker", "module", "watermark"), TIERS[1:])
async def test_a_gone_base_clears_the_watermark_for_a_full_run(
    worker: str, module: str, watermark: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = importlib.import_module(module)

    async def gone(*_: Any, **__: Any) -> None:
        raise compiler_tier.BaseUnknown("base gone")

    monkeypatch.setattr(w, "_run_repo", gone)
    repo = _repo(watermark)
    result, ctx = await _run(w, _Cfg(tmp_path), repo, owns=True)
    assert result.errors == [
        f"code-x: base gone; {watermark} cleared, the next run indexes in full"
    ]
    assert ctx.count == 0
    assert repo.set_state.await_args.args[:4] == ("o", "code-x", watermark, "")


class _RegistryCfg(_Cfg):
    source = "registry"  # a viewer added it from the Repositories page


class _RegistryNetworkCfg(_RegistryCfg):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.git_remote = "https://github.com/a/b.git"


@pytest.mark.asyncio
@pytest.mark.parametrize(("worker", "module", "watermark"), TIERS)
@pytest.mark.parametrize("sandboxed", [False, True])
async def test_without_the_sandbox_a_self_registered_network_repo_gets_no_tier(
    worker: str,
    module: str,
    watermark: str,
    sandboxed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every tier's jobs reach the network (submodules, `go mod download`,
    package installs); the plain-process fallback has no egress proxy."""
    from server.code.app.index import sandbox

    w = importlib.import_module(module)
    monkeypatch.setattr(sandbox, "_available", sandboxed)
    ran: list[str] = []

    async def run_repo(_repo: Any, cfg: Any, _result: Any) -> None:
        ran.append(cfg.namespace)

    monkeypatch.setattr(w, "_run_repo", run_repo)
    result, _ = await _run(w, _RegistryNetworkCfg(tmp_path), _repo(watermark), owns=True)
    assert ran == (["code-x"] if sandboxed else [])
    if not sandboxed:
        reason = compiler_tier.UNSANDBOXED_NETWORK_SKIP
        if worker == "code_scip_delta":
            reason = compiler_tier.UNSANDBOXED_SKIP
        assert result.skipped == [f"code-x: {reason}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("worker", "module", "watermark"), TIERS)
@pytest.mark.parametrize(
    ("cfg_type", "sandboxed", "builds"),
    [
        (_RegistryCfg, False, False),
        (_RegistryCfg, True, True),
        (_Cfg, False, True),
        (_Cfg, True, True),
    ],
    ids=["registry-unsandboxed", "registry-sandboxed", "config-unsandboxed", "config-sandboxed"],
)
async def test_a_self_registered_repo_gets_no_build_tier_without_the_sandbox(
    worker: str,
    module: str,
    watermark: str,
    cfg_type: type,
    sandboxed: bool,
    builds: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C++ tier runs the repository's CMake configure, so a self-registered
    entry gets it only when the sandbox holds; an operator's entry runs either
    way. The other tiers run static indexers (installs skip lifecycle scripts)
    and are not gated."""
    from server.code.app.index import sandbox

    w = importlib.import_module(module)
    monkeypatch.setattr(sandbox, "_available", sandboxed)
    ran: list[str] = []

    async def run_repo(_repo: Any, cfg: Any, _result: Any) -> None:
        ran.append(cfg.namespace)

    monkeypatch.setattr(w, "_run_repo", run_repo)
    result, _ = await _run(w, cfg_type(tmp_path), _repo(watermark), owns=True)
    builds = builds or worker != "code_scip_delta"
    assert ran == (["code-x"] if builds else [])
    if not builds:
        assert result.skipped == [f"code-x: {compiler_tier.UNSANDBOXED_SKIP}"]
        assert _outcome(worker)["reason"] == result.skipped[0]
