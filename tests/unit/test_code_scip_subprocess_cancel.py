"""A compiler-tier pass cancelled at its supervisor deadline (900 s, while the
harness timeout is an hour) or at shutdown kills the git / harness process it
was waiting on: nothing keeps running in the checkout the next pass reuses."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from server.code.app.index import compiler_tier
from server.code.workers import scip_go_delta as code_scip_go_delta
from server.code.workers import scip_ts_delta as code_scip_ts_delta

_CALLS: dict[str, Callable[[], Awaitable[Any]]] = {
    "git": lambda: compiler_tier.git(Path("."), "fetch"),
    "go_module_dirs": lambda: code_scip_go_delta._module_dirs(Path(".")),
    "go_changed_paths": lambda: code_scip_go_delta._changed_paths(Path("."), "a", "b"),
    "ts_lockfile_changed": lambda: code_scip_ts_delta._lockfile_changed(Path("."), "a", "b"),
}


@pytest.mark.parametrize("name", sorted(_CALLS))
async def test_cancelling_the_wait_kills_the_process(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = asyncio.create_subprocess_exec
    procs: list[asyncio.subprocess.Process] = []

    async def _sleeper(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real("sleep", "30", stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"))
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _sleeper)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await _CALLS[name]()
    assert len(procs) == 1
    try:
        async with asyncio.timeout(5):
            await procs[0].wait()
    finally:
        if procs[0].returncode is None:
            procs[0].kill()
            await procs[0].wait()
    assert procs[0].returncode is not None and procs[0].returncode < 0
