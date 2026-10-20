"""A RunScope must LABEL recall reads with the X-Loom-Run-Id header, never
scope their body (body run_id filters the read to the run's own
rows and silently empties recall; a run labels a read). Exercised on the
REAL transport (only _post_once, the HTTP boundary, is stubbed) so the header /
body split the transport applies is actually tested — and with auto_run=False,
proving run_scope alone gets it right, not just the ambient run."""

from __future__ import annotations

from typing import Any

import pytest
from loom import Agent

AMBIENT_HEADER = "X-Loom-Run-Id"


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def __call__(
        self, op_path: str, payload: dict[str, Any], headers: dict[str, str], timeout_s=None
    ):
        self.calls.append((op_path, dict(payload), dict(headers)))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-1", "status": "active"}
        if op_path == "/v1/run.complete":
            return {"run_id": "run-1", "status": payload.get("status", "completed")}
        if op_path == "/v1/memory.search":
            return {"results": [], "embedding_model": "x"}
        if op_path == "/v1/memory.ask":
            return {"answer": "", "memory_keys": [], "source": "synthesized", "abstain": True}
        return {}

    def last(self, op: str) -> tuple[dict[str, Any], dict[str, str]]:
        for o, p, h in reversed(self.calls):
            if o == op:
                return p, h
        raise AssertionError(f"no call to {op}")


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x", auto_run=False)
    a.transport._post_once = _Capture()  # type: ignore[assignment,method-assign]
    return a


async def test_recall_read_is_labelled_not_filtered(agent: Agent) -> None:
    cap: Any = agent.transport._post_once
    async with agent.run_scope(goal="fix CI"):
        await agent.memory.search("what broke ci?")
        await agent.memory.set("k1", "CI runner image was pinned")

    # search: run id is the label header, and NOT a body filter
    sbody, sheaders = cap.last("/v1/memory.search")
    assert "run_id" not in sbody, f"recall body must not carry run_id: {sbody.get('run_id')!r}"
    assert sheaders.get(AMBIENT_HEADER) == "run-1"

    # set: run id stays in the body (provenance for the write)
    wbody, _ = cap.last("/v1/memory.set")
    assert wbody.get("run_id") == "run-1"


async def test_explicit_run_filter_on_search_is_preserved(agent: Agent) -> None:
    cap: Any = agent.transport._post_once
    async with agent.run_scope(goal="g"):
        # a caller deliberately scoping the read to ANOTHER run keeps that filter
        await agent.memory.search("q", run_id="other-run")
    sbody, sheaders = cap.last("/v1/memory.search")
    assert sbody.get("run_id") == "other-run"  # explicit filter kept in body
    assert sheaders.get(AMBIENT_HEADER) == "run-1"  # active run still labelled


async def test_search_trace_is_labelled_like_search(agent: Agent) -> None:
    # search_trace reads the same scope as search: the run labels the read, it does
    # not filter the trace to the run's rows.
    cap: Any = agent.transport._post_once
    async with agent.run_scope(goal="g"):
        await agent.memory.search_trace("q")
    body, headers = cap.last("/v1/memory.search_trace")
    assert "run_id" not in body
    assert headers.get(AMBIENT_HEADER) == "run-1"
