"""`Session` (resume / append / flush / offload / fetch / note)
and `AgentTools` (definitions per provider, one dispatcher) on the Python
SDK, mirroring the TypeScript SDK. Server-free: httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import loom
import pytest
from loom import Agent, AgentTools, Session, SyncAgent
from loom.errors import BackendUnavailable, LoomError
from loom.tools import LOOM_TOOL_SPECS, tool_definitions


def _agent(replies: dict[str, Any]) -> tuple[Agent, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        op = req.url.path.split("/v1/")[1]
        body = json.loads(req.content)
        calls.append((op, body))
        r = replies.get(op, {})
        if isinstance(r, Exception):
            return httpx.Response(503, json={"detail": {"code": "X", "message": str(r)}})
        return httpx.Response(200, json=r(body) if callable(r) else r)

    a = Agent(
        org="acme",
        namespace="support",
        agent="bot",
        api_key="k",
        base_url="http://x",
        retry={"max_attempts": 1},
    )
    a.transport._client = httpx.AsyncClient(
        base_url="http://x", transport=httpx.MockTransport(handler)
    )
    return a, calls


# ---- Session -------------------------------------------------------------


async def test_session_stamps_scope_writes_the_transcript_and_flushes_on_close() -> None:
    a, calls = _agent({"memory.set_from_messages": {"written": []}})
    s = a.session("thread-1", user_id="u1", flush_every=0)
    assert isinstance(s, Session)
    out = await s.append(
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello", "name": "bot"}
    )
    assert out is None
    assert [op for op, _ in calls] == [
        "conversation.create",
        "conversation.append",
        "conversation.append",
    ]
    for _, body in calls:
        assert (
            body["session_id"] == "thread-1" and body["user_id"] == "u1" and body["org"] == "acme"
        )
    assert calls[2][1]["role"] == "assistant" and calls[2][1]["metadata"] == {"name": "bot"}
    assert len(s.buffered) == 2
    assert await s.close() == {"written": []}
    assert s.buffered == [] and calls[-1][0] == "memory.set_from_messages"
    assert await s.close() is None
    await a.close()


async def test_session_flushes_every_n_with_carry_over_and_one_thread_create() -> None:
    a, calls = _agent({"memory.set_from_messages": {"written": [{"memory_key": "k"}]}})
    s = a.session("t", flush_every=2, carry_over=1)
    assert await s.append({"role": "user", "content": "a"}) is None
    assert await s.append({"role": "assistant", "content": "b"}) == {
        "written": [{"memory_key": "k"}]
    }
    await s.append({"role": "user", "content": "c"}, {"role": "assistant", "content": "d"})
    flushes = [b for op, b in calls if op == "memory.set_from_messages"]
    assert len(flushes) == 2
    assert "last_messages" not in flushes[0]
    assert (
        flushes[1]["last_messages"] == [{"role": "assistant", "content": "b"}]
        and len(flushes[1]["messages"]) == 2
    )
    assert sum(1 for op, _ in calls if op == "conversation.create") == 1
    await a.close()


async def test_session_keeps_turns_when_extraction_fails_and_can_skip_the_transcript() -> None:
    a, calls = _agent({"memory.set_from_messages": RuntimeError("down")})
    s = a.session("t", flush_every=1, transcript=False)
    with pytest.raises(BackendUnavailable):
        await s.append({"role": "user", "content": "a"})
    assert s.buffered == [{"role": "user", "content": "a"}]
    assert all(op != "conversation.append" for op, _ in calls)
    await a.close()


async def test_session_stops_writing_the_transcript_when_logging_is_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, calls = _agent(
        {
            "conversation.append": {
                "msg_id": "m",
                "ts": "t",
                "stored": False,
                "reason": "logging_disabled",
            },
            "memory.set_from_messages": {"written": []},
        }
    )
    s = a.session("t", flush_every=0)
    assert s.transcript_stored is None
    with caplog.at_level("WARNING", logger="loom"):
        await s.append({"role": "user", "content": "a"}, {"role": "assistant", "content": "b"})
    assert s.transcript_stored is False
    assert any("conversation logging is off" in r.getMessage() for r in caplog.records)
    await s.append({"role": "user", "content": "c"})
    assert sum(1 for op, _ in calls if op == "conversation.append") == 1
    assert len(s.buffered) == 3
    await s.close()
    assert calls[-1][0] == "memory.set_from_messages"
    await a.close()


async def test_session_resume_returns_system_prompt_and_transcript_tail() -> None:
    a, calls = _agent(
        {
            "context.pack": {"rendered": "## Working memory\n- plan", "sections": {}},
            "conversation.get": lambda b: {
                "found": True,
                "thread_id": b["thread_id"],
                "messages": [{"msg_id": "1", "role": "user", "content": "hi"}],
            },
        }
    )
    r = await a.session("t", user_id="u1").resume(
        query="resume billing", token_budget=1500, history=5
    )
    assert r.system == "## Working memory\n- plan"
    assert r.messages == [{"msg_id": "1", "role": "user", "content": "hi"}]
    pack = next(b for op, b in calls if op == "context.pack")
    assert (
        pack["query"] == "resume billing"
        and pack["mode"] == "startup"
        and pack["token_budget"] == 1500
    )
    assert pack["session_id"] == "t" and pack["user_id"] == "u1"
    get = next(b for op, b in calls if op == "conversation.get")
    assert get == {**get, "thread_id": "t", "include_messages": True, "limit": 5}
    a2, calls2 = _agent({"context.pack": {"rendered": ""}, "conversation.get": {"found": False}})
    assert (await a2.session("t").resume()).messages == []
    assert (await a2.session("t", transcript=False).resume()).messages == []
    assert [op for op, _ in calls2] == ["context.pack", "conversation.get", "context.pack"]
    await a.close()
    await a2.close()


async def test_session_offload_fetch_note_and_async_context_manager() -> None:
    a, calls = _agent(
        {
            "context.offload": {
                "blob_key": "blob/abc",
                "pointer": "[offloaded → context.fetch blob_key=blob/abc] log",
            },
            "context.fetch": {
                "found": True,
                "blob_key": "blob/abc",
                "content": "x",
                "next_offset": -1,
            },
            "working_memory.append": {"block_key": "main", "version": 2},
            "memory.set_from_messages": {"written": []},
        }
    )
    async with a.session("t", flush_every=0) as s:
        blob = await s.offload("log", summary="pytest run", ttl_seconds=60)
        assert "blob/abc" in blob["pointer"]
        assert (
            calls[0][1]["content"] == "log"
            and calls[0][1]["ttl_seconds"] == 60
            and calls[0][1]["session_id"] == "t"
        )
        got = await s.fetch("blob/abc", pattern="FAILED", context_lines=2)
        assert (
            got["content"] == "x"
            and calls[1][1]["pattern"] == "FAILED"
            and calls[1][1]["context_lines"] == 2
        )
        assert (await s.note("plan: billing first"))["version"] == 2
        await s.append({"role": "user", "content": "bye"})
    assert calls[-1][0] == "memory.set_from_messages"  # __aexit__ flushed
    await a.close()


def test_sync_agent_session_and_tools_block() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        op = req.url.path.split("/v1/")[1]
        if op == "context.pack":
            return httpx.Response(200, json={"rendered": "sys"})
        if op == "memory.search":
            return httpx.Response(
                200,
                json={"results": [{"memory_key": "k", "content_excerpt": "v", "similarity": 0.5}]},
            )
        return httpx.Response(200, json={"found": False})

    with SyncAgent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x") as sa:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x", transport=httpx.MockTransport(handler)
        )
        s = sa.session("t", transcript=False)
        assert s.resume().system == "sys"
        tools = sa.tools(user_id="u")
        assert json.loads(tools.execute("loom_recall", {"query": "q"})) == {
            "memories": [{"memory_key": "k", "content": "v", "similarity": 0.5}]
        }
        assert tools.has("loom_ask") and len(tools.definitions("openai")) == 6


# ---- tools ---------------------------------------------------------------


def test_tool_definitions_in_three_shapes_match_the_typescript_sdk() -> None:
    assert [s["name"] for s in LOOM_TOOL_SPECS] == [
        "loom_remember",
        "loom_recall",
        "loom_ask",
        "loom_offload",
        "loom_fetch",
        "loom_note",
    ]
    a = tool_definitions("anthropic")
    assert a[0]["name"] == "loom_remember" and a[0]["input_schema"]["required"] == [
        "memory_key",
        "value",
    ]
    o = tool_definitions("openai", only=["loom_ask"])
    assert len(o) == 1 and o[0]["type"] == "function" and o[0]["function"]["name"] == "loom_ask"
    assert len(tool_definitions()) == 6
    for s in LOOM_TOOL_SPECS:
        assert len(s["description"]) > 40 and s["parameters"]["additionalProperties"] is False
        for r in s["parameters"]["required"]:
            assert r in s["parameters"]["properties"]
    assert loom.LOOM_TOOL_SPECS is LOOM_TOOL_SPECS and loom.tool_definitions is tool_definitions


async def test_tools_execute_runs_each_verb_under_the_scope() -> None:
    a, calls = _agent(
        {
            "memory.set": {"memory_key": "seat", "version": 3, "conflict_verdict": "new"},
            "memory.search": {
                "results": [
                    {
                        "memory_key": "seat",
                        "content_excerpt": "Prefers an aisle seat",
                        "similarity": 0.51234,
                    }
                ]
            },
            "memory.ask": {"answer": "An aisle seat.", "abstain": False, "memory_keys": ["seat"]},
            "context.offload": {
                "blob_key": "blob/1",
                "pointer": "[offloaded → context.fetch blob_key=blob/1] log",
            },
            "context.fetch": {
                "found": True,
                "blob_key": "blob/1",
                "content": "line",
                "next_offset": -1,
                "matched_lines": 1,
            },
            "working_memory.append": {"block_key": "main", "version": 1, "fill_ratio": 0.01},
        }
    )
    tools = a.tools(user_id="u1", session_id="s1")
    assert isinstance(tools, AgentTools)
    assert json.loads(
        await tools.execute(
            "loom_remember",
            {"memory_key": "seat", "value": "Prefers an aisle seat", "memory_type": "preference"},
        )
    ) == {
        "stored": True,
        "memory_key": "seat",
        "version": 3,
        "verdict": "new",
    }
    assert (
        calls[0][1]["memory_type"] == "preference"
        and calls[0][1]["user_id"] == "u1"
        and calls[0][1]["session_id"] == "s1"
    )
    assert json.loads(await tools.execute("loom_recall", '{"query":"seat","top_k":3}')) == {
        "memories": [
            {"memory_key": "seat", "content": "Prefers an aisle seat", "similarity": 0.512}
        ]
    }
    assert calls[1][1]["top_k"] == 3
    assert json.loads(await tools.execute("loom_ask", {"query": "which seat?"})) == {
        "answer": "An aisle seat.",
        "abstain": False,
        "cited": ["seat"],
    }
    assert "blob_key=blob/1" in await tools.execute(
        "loom_offload", {"content": "log", "summary": "a log"}
    )
    assert json.loads(
        await tools.execute("loom_fetch", {"blob_key": "blob/1", "pattern": "FAILED"})
    ) == {"content": "line", "next_offset": -1, "matched_lines": 1}
    assert json.loads(await tools.execute("loom_note", {"addition": "plan"}))["noted"] is True
    await a.close()


async def test_tools_reject_unknown_bad_json_and_missing_args_and_truncate() -> None:
    a, calls = _agent(
        {
            "context.fetch": lambda b: (
                {"found": False, "blob_key": b["blob_key"]}
                if b["blob_key"] == "nope"
                else {"found": True, "blob_key": "b", "content": "x" * 500, "next_offset": -1}
            )
        }
    )
    tools = a.tools(only=["loom_recall", "loom_fetch"], max_result_chars=100)
    assert len(tools.definitions()) == 2
    with pytest.raises(
        LoomError, match="unknown tool loom_remember; this agent offers loom_recall, loom_fetch"
    ):
        await tools.execute("loom_remember", {})
    with pytest.raises(LoomError, match="not valid JSON"):
        await tools.execute("loom_recall", "{not json")
    with pytest.raises(LoomError, match="query is required"):
        await tools.execute("loom_recall", {"query": ""})
    with pytest.raises(LoomError, match='top_k must be an integer, got "five"'):
        await tools.execute("loom_recall", {"query": "seat", "top_k": "five"})
    with pytest.raises(LoomError, match="offset must be an integer, got 1.5"):
        await tools.execute("loom_fetch", {"blob_key": "b", "offset": 1.5})
    await tools.execute("loom_recall", {"query": "seat", "top_k": 5.0})
    await tools.execute("loom_recall", {"query": "seat", "top_k": "5.0"})
    assert [b["top_k"] for op, b in calls if op == "memory.search"] == [5, 5]
    assert "no parked text under blob_key=nope" in await tools.execute(
        "loom_fetch", {"blob_key": "nope"}
    )
    out = await tools.execute("loom_fetch", {"blob_key": "b"})
    assert out.endswith("…[truncated]") and len(out) < 120
    await a.close()


def test_sync_session_is_a_with_block_and_task_stream_iterates() -> None:
    seen: list[str] = []
    pulls = iter([[{"task_id": "t1"}, {"task_id": "t2"}], [{"task_id": "t3"}]])

    def handler(req: httpx.Request) -> httpx.Response:
        op = req.url.path.split("/v1/")[1]
        seen.append(op)
        if op == "task.pull":
            return httpx.Response(200, json={"tasks": next(pulls)})
        return httpx.Response(200, json={"written": []})

    with SyncAgent(org="o", namespace="n", agent="a", api_key="k", base_url="http://x") as sa:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x", transport=httpx.MockTransport(handler)
        )
        with sa.session("t", transcript=False, flush_every=0) as s:
            s.append({"role": "user", "content": "hi"})
            assert "memory.set_from_messages" not in seen
        assert seen[-1] == "memory.set_from_messages"  # the with block flushed
        got = []
        for task in sa.tasks.stream("inbox"):
            got.append(task["task_id"])
            if len(got) == 3:
                break
        assert got == ["t1", "t2", "t3"]
