"""Unit coverage for the SDK trace-and-resume helpers.

`checkpoint`/`resume` are named wrappers over run-scoped working memory. Server-free:
a fake transport records the calls so we verify the right ops, run-scoping, and
return shapes without a live stack.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loom import Agent


class _FakeTransport:
    def __init__(self, get_result: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._get_result = get_result

    async def post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        if url.endswith("working_memory.get"):
            return self._get_result
        return {"ok": True}

    async def close(self) -> None:
        pass


def _agent(get_result: dict[str, Any]) -> Agent:
    a = Agent(org="dev", namespace="ns", agent="bot", api_key="t")
    a.transport = _FakeTransport(get_result)  # type: ignore[assignment]
    return a


def test_checkpoint_sets_run_scoped_block() -> None:
    a = _agent({"found": False})
    asyncio.run(a.working_memory.checkpoint("done: 1..150", run_id="run-7"))
    url, payload = a.transport.calls[-1]  # type: ignore[attr-defined]
    assert url.endswith("/v1/working_memory.set")
    assert payload["run_id"] == "run-7"  # run is the resume key
    assert payload["block_key"] == "checkpoint"
    assert payload["content"] == "done: 1..150"


def test_resume_returns_last_checkpoint() -> None:
    a = _agent({"found": True, "content": "done: 1..150"})
    out = asyncio.run(a.working_memory.resume(run_id="run-7"))
    assert out == "done: 1..150"
    url, payload = a.transport.calls[-1]  # type: ignore[attr-defined]
    assert url.endswith("/v1/working_memory.get")
    assert payload["run_id"] == "run-7"
    assert payload["block_key"] == "checkpoint"


def test_resume_is_none_for_a_fresh_run() -> None:
    a = _agent({"found": False})
    out = asyncio.run(a.working_memory.resume(run_id="brand-new"))
    assert out is None  # caller starts from scratch
