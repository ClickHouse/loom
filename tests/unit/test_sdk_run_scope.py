"""SDK run scope: one `async with` block is one run, and the
memories its recalls cited are the run's `memories_used`."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from loom import Agent
from loom.run import current_run, current_run_id


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_complete = False

    async def post(
        self, op_path: str, payload: dict[str, Any], idempotency_key=None, *, timeout_s=None
    ):
        self.calls.append((op_path, payload))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-1"}
        if op_path == "/v1/run.complete":
            if self.fail_complete:
                raise RuntimeError("ledger down")
            return {"run_id": payload["run_id"], "status": payload["status"]}
        if op_path == "/v1/memory.ask":
            return {
                "answer": "Blue [m1].",
                "memory_keys": ["m1"],
                "source": "synthesized",
                "abstain": False,
            }
        if op_path == "/v1/memory.search":
            return {"results": [], "embedding_model": "x"}
        return {}

    async def close(self) -> None:  # pragma: no cover
        pass

    def payload(self, op: str) -> dict[str, Any]:
        return [p for o, p in self.calls if o == op][-1]


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x")
    a.transport = _FakeTransport()  # type: ignore[assignment]
    return a


async def test_scope_begins_credits_cited_keys_and_completes(agent: Agent) -> None:
    t: Any = agent.transport
    assert current_run() is None
    async with agent.run_scope(goal="fix CI") as run:
        assert current_run_id() == "run-1" and run.run_id == "run-1"
        out = await agent.memory.ask("what broke CI?")
        assert out["memory_keys"] == ["m1"]
        run.used("k_explicit")
        run.final_response = "Pinned the image."
        await agent.memory.search("ci")  # stamps the run id
    assert current_run() is None
    assert t.payload("/v1/run.begin")["goal"] == "fix CI"
    assert t.payload("/v1/memory.ask")["run_id"] == "run-1"
    assert t.payload("/v1/memory.search")["run_id"] == "run-1"
    done = t.payload("/v1/run.complete")
    assert done["status"] == "completed" and done["memories_used"] == ["m1", "k_explicit"]
    assert done["outcome"] == "positive" and done["final_response"] == "Pinned the image."
    assert run.completed == {"run_id": "run-1", "status": "completed"}


async def test_exception_completes_as_failed_and_propagates(agent: Agent) -> None:
    t: Any = agent.transport
    with pytest.raises(ValueError):
        async with agent.run_scope(goal="g") as run:
            await agent.memory.ask("q")
            raise ValueError("boom")
    done = t.payload("/v1/run.complete")
    assert (
        done["status"] == "failed"
        and done["outcome"] == "negative"
        and done["memories_used"] == ["m1"]
    )
    assert current_run() is None and run.completed is not None


async def test_cancellation_completes_as_cancelled_and_propagates(agent: Agent) -> None:
    t: Any = agent.transport
    with pytest.raises(asyncio.CancelledError):
        async with agent.run_scope(goal="g"):
            raise asyncio.CancelledError
    done = t.payload("/v1/run.complete")
    # Cancelled judges nothing: the server derives outcome `none` from the status.
    assert done["status"] == "cancelled" and done["outcome"] == ""


async def test_a_clean_exit_closes_with_the_status_the_block_set(agent: Agent) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="g", budget_seconds=60) as run:
        run.status = "cancelled"  # stopped early, e.g. over budget
    assert t.payload("/v1/run.begin")["budget_seconds"] == 60
    assert t.payload("/v1/run.complete")["status"] == "cancelled"


async def test_ledger_failure_never_masks_the_block(agent: Agent) -> None:
    t: Any = agent.transport
    t.fail_complete = True
    async with agent.run_scope(goal="g") as run:
        pass
    assert run.completed is None and current_run() is None


async def test_abstained_recall_credits_nothing_and_explicit_outcome_wins(agent: Agent) -> None:
    t: Any = agent.transport

    async def post(op_path, payload, idempotency_key=None, *, timeout_s=None):
        t.calls.append((op_path, payload))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-2"}
        if op_path == "/v1/memory.ask":
            return {
                "answer": "",
                "memory_keys": ["m1", "m2"],
                "source": "abstained",
                "abstain": True,
            }
        return {"run_id": "run-2", "status": payload.get("status", "")}

    t.post = post  # type: ignore[method-assign]
    async with agent.run_scope(goal="g") as run:
        await agent.memory.ask("q")
        run.outcome = "none"
    done = t.payload("/v1/run.complete")
    assert done["memories_used"] == [] and done["outcome"] == "none"


async def test_outside_a_scope_nothing_changes(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.memory.search("ci")
    assert "run_id" not in t.payload("/v1/memory.search")
    out = await agent.memory.ask("q")
    assert out["memory_keys"] == ["m1"] and t.payload("/v1/memory.ask")["run_id"] == "-"


async def test_note_and_append_note_write_the_runs_scratchpad(agent: Agent) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="g") as run:
        await run.note("Goal: fix CI")
        await run.append_note("Done: pinned image")
    p = [p for o, p in t.calls if o == "/v1/working_memory.set"][-1]
    assert (
        p["block_key"] == "scratchpad" and p["content"] == "Goal: fix CI" and p["run_id"] == "run-1"
    )
    a = [p for o, p in t.calls if o == "/v1/working_memory.append"][-1]
    assert a["addition"] == "Done: pinned image" and a["run_id"] == "run-1"


@pytest.mark.parametrize("entry", ["call", "session", "tools", "note"])
async def test_writes_through_agent_call_carry_the_scope_run_id(agent: Agent, entry: str) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="g"):
        if entry == "call":
            await agent.call("memory.set", {"memory_key": "k", "value": "v"})
        elif entry == "session":
            s = agent.session("thread-1", transcript=False, flush_every=0)
            await s.append([{"role": "user", "content": "hi"}])
            await s.flush()
        elif entry == "tools":
            await agent.tools().execute("loom_remember", {"memory_key": "k", "value": "v"})
        else:
            await agent.tools().execute("loom_note", {"addition": "x"})
    op = {
        "call": "/v1/memory.set",
        "session": "/v1/memory.set_from_messages",
        "tools": "/v1/memory.set",
        "note": "/v1/working_memory.append",
    }[entry]
    assert t.payload(op)["run_id"] == "run-1"


async def test_a_typed_update_inside_a_scope_carries_the_run(agent: Agent) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="g"):
        await agent.memory.update("k", "v")
    assert t.payload("/v1/memory.update")["run_id"] == "run-1"
    await agent.memory.update("k", "v2")
    assert "run_id" not in t.payload("/v1/memory.update")


async def test_call_keeps_an_explicit_run_id_and_reads_are_not_stamped(agent: Agent) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="g"):
        await agent.call("memory.set", {"memory_key": "k"}, run_id="other")
        await agent.call("conversation.search", {"query": "q"})
    assert t.payload("/v1/memory.set")["run_id"] == "other"
    assert t.payload("/v1/conversation.search")["run_id"] == "-"
    await agent.call("memory.set", {"memory_key": "k"})
    assert t.payload("/v1/memory.set")["run_id"] == "-"


@pytest.mark.parametrize("entry", ["memory.ask", "tools"])
async def test_every_ask_entry_point_credits_the_cited_keys(agent: Agent, entry: str) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="g"):
        if entry == "tools":
            await agent.tools().execute("loom_ask", {"query": "q"})
        else:
            await agent.memory.ask("q")
    assert t.payload("/v1/run.complete")["memories_used"] == ["m1"]
