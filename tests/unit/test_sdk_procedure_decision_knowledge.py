"""The procedure run ledger records the caller's session, a decision recorded
inside a run_scope links to that run like every memory write, and a knowledge
hit says which namespace and section it came from."""

from __future__ import annotations

from typing import Any

import pytest
from loom import Agent
from loom.knowledge import _hit


class _Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, op_path: str, payload: dict[str, Any], headers: Any, timeout_s=None):
        self.calls.append((op_path, dict(payload)))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-1", "status": "active"}
        return {}

    def last(self, op: str) -> dict[str, Any]:
        return [p for o, p in self.calls if o == op][-1]


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x", auto_run=False)
    a.transport._post_once = _Capture()  # type: ignore[assignment,method-assign]
    return a


async def test_record_run_sends_the_callers_session(agent: Agent) -> None:
    cap: Any = agent.transport._post_once
    await agent.procedure.record_run("proc/deploy", ["a", "b"], session_id="sess-9")
    assert cap.last("/v1/procedure.record_run")["session_id"] == "sess-9"
    await agent.procedure.record_run("proc/deploy", ["a"])
    assert cap.last("/v1/procedure.record_run")["session_id"] == "-"


async def test_decision_inside_a_run_scope_links_to_the_run(agent: Agent) -> None:
    cap: Any = agent.transport._post_once
    kw: dict[str, Any] = dict(category="c", scenario="s", reasoning="r", outcome="o")
    async with agent.run_scope(goal="g"):
        await agent.decision.record(**kw)
        assert cap.last("/v1/decision.record")["run_id"] == "run-1"
        await agent.decision.record(**kw, run_id="other")
        assert cap.last("/v1/decision.record")["run_id"] == "other"
    await agent.decision.record(**kw)
    assert "run_id" not in cap.last("/v1/decision.record")


def test_knowledge_hit_keeps_namespace_and_section() -> None:
    base = {"source_uri": "u", "chunk_idx": 0, "content": "c", "tags": []}
    base |= {"distance": 0.2, "similarity": 0.8}
    hit = _hit(base | {"namespace": "team-a", "section_path": ["A", "B"], "section": "A > B"})
    assert (hit.namespace, hit.section_path, hit.section) == ("team-a", ("A", "B"), "A > B")
    assert (_hit(base).namespace, _hit(base).section_path) == ("", ())


async def test_decision_record_sends_files_and_refuses_a_bare_string(agent: Agent) -> None:
    cap: Any = agent.transport._post_once
    kw: dict[str, Any] = dict(category="c", scenario="s", reasoning="r", outcome="o")
    await agent.decision.record(
        **kw, files=("src/a.cpp",), source_class="derived", source_resource_id="doc:1"
    )
    sent = cap.last("/v1/decision.record")
    assert sent["files"] == ["src/a.cpp"] and sent["source_class"] == "derived"
    assert sent["source_resource_id"] == "doc:1" and "stage" not in sent
    await agent.decision.record(**kw, stage="proposed")
    assert cap.last("/v1/decision.record")["stage"] == "proposed"
    with pytest.raises(TypeError):
        await agent.decision.record(**kw, files="src/a.cpp")
