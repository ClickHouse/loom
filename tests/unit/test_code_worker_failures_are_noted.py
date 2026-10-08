"""A code or tracker worker that isolates one repository's failure notes it on the pass.

The supervisor classifies a pass `partial`, counts it toward the failure streak and
publishes `loom.worker.partial_failures` only from the failures noted on the pass
context; a failure that reaches only `result.errors` leaves the pass `ok` while a
repository fails every pass.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.workers import pass_context
from server.workers.status_store import classify


class _Cfg:
    org = "o"
    namespace = "code-x"
    label = "o/code-x"
    repo = "Org/Repo"
    provider = "github"
    github_token = "tok"
    git_dir = ""


async def _boom(*_: Any, **__: Any) -> Any:
    raise RuntimeError("repo down")


async def _owns(*_: Any, **__: Any) -> bool:
    return True


async def _noted(run: Any) -> pass_context.PassContext:
    ctx = pass_context.begin("w")
    try:
        await run()
    finally:
        pass_context.clear()
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker", "module"),
    [
        ("code_scip_delta", "server.code.workers.scip_delta"),
        ("code_scip_ts_delta", "server.code.workers.scip_ts_delta"),
        ("code_scip_py_delta", "server.code.workers.scip_py_delta"),
        ("code_scip_go_delta", "server.code.workers.scip_go_delta"),
    ],
)
async def test_a_compiler_tier_repo_failure_makes_the_pass_partial(
    worker: str, module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from server.code.app.index import compiler_tier

    w = importlib.import_module(module)
    monkeypatch.setattr(w, "_run_repo", _boom)
    monkeypatch.setattr(compiler_tier, "worker_owns", _owns)
    with (
        patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
        patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
    ):
        if worker == "code_scip_delta":
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _lock(*_: Any, **__: Any) -> Any:
                yield

            monkeypatch.setattr(compiler_tier, "entry_lock", _lock)
        ctx = await _noted(w.run_pass)
    assert ctx.count == 1
    assert ctx.failures[0][:2] == ("code-x", "RuntimeError")
    assert classify(success=True, partial_failures=ctx.count) == "partial"
    # Every tier names the failure in its reason, not "current".
    from server.workers.status import get_registry

    outcome = get_registry().snapshot()[worker]["last_outcome"]
    assert outcome["errors"] == 1 and outcome["reason"] == "error"


def test_a_tier_outcome_keeps_the_tiers_own_counters() -> None:
    from server.code.app.index.compiler_tier import tier_outcome
    from server.code.workers.scip_delta import CodeScipDeltaResult

    r = CodeScipDeltaResult(repos_checked=2, deltas_applied=1, skipped=["s"], errors=["e"])
    out = tier_outcome(r, modules_indexed=3)
    assert out == {
        "repos_checked": 2,
        "deltas_applied": 1,
        "modules_indexed": 3,
        "skipped": ["s"],
        "errors": 1,
        "reason": "applied",
    }
    assert tier_outcome(CodeScipDeltaResult())["reason"] == "current"


@pytest.mark.asyncio
async def test_a_pr_history_repo_failure_makes_the_pass_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.workers import pr_history as w

    monkeypatch.setattr(w, "_sync_repo", _boom)
    with (
        patch("server.code.store.index.CodeRepo", return_value=AsyncMock()),
        patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
    ):
        ctx = await _noted(w.run_pass)
    assert ctx.count == 1 and ctx.failures[0][0] == "code-x"


@pytest.mark.asyncio
async def test_a_tracker_daily_namespace_failure_makes_the_pass_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.tracker.workers import test_daily as w

    monkeypatch.setattr("server.tracker.domain.sources.credential_configured", lambda: True)
    monkeypatch.setattr(w, "_repo", lambda: AsyncMock())
    monkeypatch.setattr(w, "_namespaces", lambda: [("o", "qa-x")])
    monkeypatch.setattr(w, "roll_namespace", _boom)
    ctx = await _noted(w.run_pass)
    assert ctx.count == 1 and ctx.failures[0][0] == "qa-x"
