"""SDK ambient run: an `Agent(auto_run=True)` binds a run to
its lifetime — begun lazily on the first memory-plane call, writes stamped
with its `run_id`, reads labelled with `X-Loom-Run-Id` (never the filtering
field), completed on close. Off by default; an explicit `run_scope` wins."""

from __future__ import annotations

from typing import Any

import pytest
from loom import Agent
from loom.run import AMBIENT_RUN_HEADER, AmbientRun
from loom.transport import Transport


class _Recorder:
    """Stands in for httpx: records (path, payload, headers) per POST."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.fail_begin = False

    async def post_once(
        self, op_path: str, payload: dict[str, Any], headers: dict[str, str], timeout_s
    ):
        self.calls.append((op_path, dict(payload), dict(headers)))
        if op_path == "/v1/run.begin":
            if self.fail_begin:
                raise RuntimeError("ledger down")
            return {"run_id": "amb-1", "status": "active"}
        if op_path == "/v1/run.complete":
            return {"run_id": payload["run_id"], "status": payload["status"]}
        if op_path == "/v1/memory.search":
            return {"results": [], "embedding_model": "x"}
        return {"version": 1}

    def by_path(self, path: str):
        return [c for c in self.calls if c[0] == path]


@pytest.fixture
def agent(monkeypatch):
    a = Agent(org="o", namespace="n", agent="ag", api_key="x", auto_run=True)
    rec = _Recorder()
    monkeypatch.setattr(Transport, "_post_once", lambda self, *a, **k: rec.post_once(*a, **k))
    a._rec = rec  # type: ignore[attr-defined]
    return a


def test_default_is_off_and_env_turns_it_on(monkeypatch) -> None:
    monkeypatch.delenv("LOOM_AUTO_RUN", raising=False)
    a = Agent(org="o", namespace="n", agent="ag", api_key="x")
    assert a.auto_run is False and a.ambient_run is None and a.ambient_run_id == ""
    assert a.transport.before_post is None
    monkeypatch.setenv("LOOM_AUTO_RUN", "true")
    b = Agent(org="o", namespace="n", agent="ag", api_key="x")
    assert b.auto_run is True and isinstance(b.ambient_run, AmbientRun)
    assert b.transport.before_post == b.ambient_run.before_post
    c = Agent(org="o", namespace="n", agent="ag", api_key="x", auto_run=False)
    assert c.ambient_run is None


async def test_first_memory_call_begins_the_run_writes_stamped_reads_labelled(agent: Agent) -> None:
    rec: Any = agent._rec  # type: ignore[attr-defined]
    async with agent:
        # A non-memory call does not open the run.
        await agent.transport.post("/v1/grants.list", {"org": "o"})
        assert agent.ambient_run_id == "" and rec.by_path("/v1/run.begin") == []
        # A read opens it: goal = the query; the read is labelled, not stamped.
        await agent.memory.search("what broke CI")
        assert agent.ambient_run_id == "amb-1"
        begin = rec.by_path("/v1/run.begin")[0][1]
        assert begin["goal"] == "what broke CI" and begin["environment"] == "sdk"
        assert begin["consumer"] == "ag" and begin["metadata"] == {
            "implicit": True,
            "transport": "sdk",
        }
        path, payload, headers = rec.by_path("/v1/memory.search")[0]
        assert payload.get("run_id", "-") == "-", (
            "a read's run_id filters; the ambient run must not set it"
        )
        assert headers[AMBIENT_RUN_HEADER] == "amb-1"
        # A write is stamped and carries no header.
        await agent.memory.set("ci:flaky", "pin the runner image")
        _, payload, headers = rec.by_path("/v1/memory.set")[0]
        assert payload["run_id"] == "amb-1" and AMBIENT_RUN_HEADER not in headers
        # An explicit run_id on a write is kept.
        await agent.memory.set("k2", "v", run_id="mine")
        assert rec.by_path("/v1/memory.set")[1][1]["run_id"] == "mine"
    done = rec.by_path("/v1/run.complete")
    assert len(done) == 1
    assert done[0][1]["run_id"] == "amb-1" and done[0][1]["status"] == "completed"
    assert done[0][1]["outcome"] == "none" and done[0][1]["metadata"]["closed_by"] == "sdk_close"
    assert done[0][1]["metadata"]["calls"] == 3
    assert agent.ambient_run is not None and agent.ambient_run.completed is not None


async def test_an_exception_completes_it_failed_and_close_is_idempotent(agent: Agent) -> None:
    rec: Any = agent._rec  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        async with agent:
            await agent.memory.set("k", "v")
            raise ValueError("boom")
    done = rec.by_path("/v1/run.complete")
    assert (
        len(done) == 1 and done[0][1]["status"] == "failed" and done[0][1]["outcome"] == "negative"
    )
    await agent.close()
    assert len(rec.by_path("/v1/run.complete")) == 1


async def test_no_memory_call_no_ledger_traffic_and_begin_failure_is_silent(agent: Agent) -> None:
    rec: Any = agent._rec  # type: ignore[attr-defined]
    async with agent:
        pass
    assert rec.calls == []
    rec.fail_begin = True
    async with agent:
        await agent.memory.set("k", "v")  # the write still goes through, unstamped
        assert agent.ambient_run_id == ""
        await agent.memory.set("k2", "v")
    assert len(rec.by_path("/v1/run.begin")) == 1, "one attempt, not one per call"
    assert all(c[1].get("run_id", "-") == "-" for c in rec.by_path("/v1/memory.set"))
    assert rec.by_path("/v1/run.complete") == []


async def test_an_explicit_run_scope_wins_inside_the_block(agent: Agent) -> None:
    rec: Any = agent._rec  # type: ignore[attr-defined]

    async def _post_once(self, op_path, payload, headers, timeout_s):
        if op_path == "/v1/run.begin" and payload.get("goal") == "scoped":
            rec.calls.append((op_path, dict(payload), dict(headers)))
            return {"run_id": "scope-1"}
        return await rec.post_once(op_path, payload, headers, timeout_s)

    from unittest.mock import patch

    with patch.object(Transport, "_post_once", _post_once):
        async with agent:
            async with agent.run_scope(goal="scoped"):
                await agent.memory.set("k", "v")
                await agent.memory.search("q")
            assert agent.ambient_run_id == "", "the scope covered every call; no ambient run opened"
            await agent.memory.search("outside")
            assert agent.ambient_run_id == "amb-1"
    sets = rec.by_path("/v1/memory.set")
    assert sets[0][1]["run_id"] == "scope-1" and AMBIENT_RUN_HEADER not in sets[0][2]
    searches = rec.by_path("/v1/memory.search")
    # A recall read under the scope is LABELLED by the header, never scoped in
    # the body (a body run_id would filter it to the run's own rows and empty recall) —
    # the same split the ambient run applies to its own reads below.
    assert "run_id" not in searches[0][1] and searches[0][2][AMBIENT_RUN_HEADER] == "scope-1"
    assert searches[1][2][AMBIENT_RUN_HEADER] == "amb-1"


def test_sync_agent_passes_auto_run_through() -> None:
    import inspect

    from loom.sync import SyncAgent

    assert "auto_run" in inspect.signature(SyncAgent.__init__).parameters
