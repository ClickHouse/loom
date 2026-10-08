"""`SyncAgent.run` is a blocking proxy of the run ledger and
`SyncAgent.run_scope` is the `with` form of `Agent.run_scope` — memory calls
from the entering thread carry the run_id and the exit completes the run."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from loom import SyncAgent


@pytest.fixture
def sa():
    calls: list[tuple[str, dict[str, Any]]] = []
    agent = SyncAgent(org="o", namespace="n", agent="a", api_key="x")

    async def fake_post(op_path, payload, idempotency_key=None, *, timeout_s=None):
        await asyncio.sleep(0)
        calls.append((op_path, payload))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-1", "status": "active"}
        if op_path == "/v1/run.complete":
            return {"run_id": payload["run_id"], "status": payload["status"], "used_applied": 1}
        if op_path == "/v1/run.get":
            return {"found": True, "run": {"run_id": payload["run_id"]}}
        if op_path == "/v1/memory.ask":
            return {
                "answer": "x",
                "memory_keys": ["cited"],
                "source": "synthesized",
                "abstain": False,
            }
        if op_path == "/v1/memory.set":
            return {"version": 1}
        return {}

    agent._agent.transport.post = fake_post  # type: ignore[method-assign]
    agent.calls = calls  # type: ignore[attr-defined]
    try:
        yield agent
    finally:
        agent.close()


def _payload(sa: Any, op: str) -> dict[str, Any]:
    return [p for o, p in sa.calls if o == op][-1]


def test_run_client_is_proxied_and_blocking(sa: Any) -> None:
    out = sa.run.begin(goal="fix CI")
    assert out["run_id"] == "run-1"
    assert sa.run.get("run-1")["found"] is True
    assert threading.current_thread() is threading.main_thread()


def test_run_scope_begins_stamps_and_completes(sa: Any) -> None:
    with sa.run_scope(goal="fix CI", user_request="please") as run:
        assert run.run_id == "run-1"
        sa.memory.set("k", 1)
        sa.memory.ask("what broke?")
        run.used("explicit")
        run.final_response = "done"
    assert _payload(sa, "/v1/run.begin")["goal"] == "fix CI"
    assert _payload(sa, "/v1/memory.set")["run_id"] == "run-1"
    assert _payload(sa, "/v1/memory.ask")["run_id"] == "run-1"
    done = _payload(sa, "/v1/run.complete")
    assert done["status"] == "completed" and done["final_response"] == "done"
    assert sorted(done["memories_used"]) == ["cited", "explicit"]
    assert run.completed is not None and run.completed["used_applied"] == 1


def test_run_scope_marks_a_raising_block_failed_and_reraises(sa: Any) -> None:
    with pytest.raises(ValueError):
        with sa.run_scope(goal="g"):
            raise ValueError("boom")
    done = _payload(sa, "/v1/run.complete")
    assert done["status"] == "failed" and done["outcome"] == "negative"


def test_run_scope_leaves_no_active_run_behind(sa: Any) -> None:
    from loom.run import current_run

    with sa.run_scope(goal="g"):
        assert current_run() is not None
    assert current_run() is None
    sa.memory.set("k", 1)
    assert _payload(sa, "/v1/memory.set")["run_id"] == "-"


def test_run_scope_through_the_run_proxy_is_the_blocking_form(sa: Any) -> None:
    with sa.run.scope(goal="g") as run:
        sa.memory.set("k", "v")
    assert run.run_id == "run-1" and _payload(sa, "/v1/memory.set")["run_id"] == "run-1"
    assert _payload(sa, "/v1/run.complete")["status"] == "completed"
