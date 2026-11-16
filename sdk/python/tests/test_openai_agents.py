"""loom_tools: Loom's agent verbs as OpenAI Agents SDK FunctionTools over a
fake Loom agent — names and schemas, the scope each call carries, and the
string the model reads back."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import Any

import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext
from loom import Agent
from loom.errors import LoomError
from loom.integrations.openai_agents import loom_tools
from loom.tools import LOOM_TOOL_SPECS


class _FakeAgent:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self._reply = reply

    async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
        self.calls.append((op, dict(body), scope))
        return self._reply


def _invoke(tool: FunctionTool, arguments: dict[str, Any]) -> Any:
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    return asyncio.run(tool.on_invoke_tool(ctx, raw))


def test_every_verb_is_a_function_tool_with_looms_schema() -> None:
    tools = loom_tools(_FakeAgent({}))  # type: ignore[arg-type]
    assert all(isinstance(t, FunctionTool) for t in tools)
    assert [t.name for t in tools] == [s["name"] for s in LOOM_TOOL_SPECS]
    for tool, spec in zip(tools, LOOM_TOOL_SPECS, strict=True):
        assert tool.description == spec["description"]
        assert tool.params_json_schema == spec["parameters"]
        assert tool.strict_json_schema is False  # optional parameters stay optional


def test_only_keeps_a_subset() -> None:
    tools = loom_tools(_FakeAgent({}), only=["loom_recall", "loom_remember"])  # type: ignore[arg-type]
    assert [t.name for t in tools] == ["loom_remember", "loom_recall"]


def test_recall_runs_memory_search_in_the_bound_scope() -> None:
    agent = _FakeAgent(
        {
            "results": [
                {"memory_key": "seat", "content_excerpt": "Alice prefers aisle", "similarity": 0.91}
            ]
        }
    )
    (recall,) = loom_tools(agent, user_id="alice", session_id="s-1", only=["loom_recall"])  # type: ignore[arg-type]
    out = _invoke(recall, {"query": "seat preference", "top_k": 3})
    assert agent.calls == [
        (
            "memory.search",
            {"query": "seat preference", "bodies": "full", "explain": False, "top_k": 3},
            {"user_id": "alice", "session_id": "s-1", "run_id": None},
        )
    ]
    assert json.loads(out) == {
        "memories": [{"memory_key": "seat", "content": "Alice prefers aisle", "similarity": 0.91}]
    }


def test_remember_runs_memory_set() -> None:
    agent = _FakeAgent({"version": 2})
    (remember,) = loom_tools(agent, only=["loom_remember"])  # type: ignore[arg-type]
    out = _invoke(remember, {"memory_key": "seat", "value": "Alice prefers aisle seats"})
    assert agent.calls[0][:2] == (
        "memory.set",
        {"memory_key": "seat", "value": "Alice prefers aisle seats"},
    )
    assert json.loads(out)["stored"] is True


def test_without_openai_agents_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "agents", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.openai_agents")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[openai-agents\]"):
        importlib.import_module("loom.integrations.openai_agents")


def test_a_real_agent_posts_memory_search_in_the_bound_scope() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    agent = Agent(org="acme", namespace="support", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = post  # type: ignore[method-assign]
    (recall,) = loom_tools(agent, user_id="alice", only=["loom_recall"])
    assert json.loads(_invoke(recall, {"query": "seat", "top_k": 2})) == {"memories": []}
    ((path, body),) = posts
    assert path == "/v1/memory.search"
    assert (body["query"], body["top_k"], body["user_id"]) == ("seat", 2, "alice")


def test_a_refused_call_returns_the_error_to_the_model() -> None:
    (recall,) = loom_tools(_FakeAgent({}), only=["loom_recall"])  # type: ignore[arg-type]
    out = _invoke(recall, {"query": ""})
    assert out.startswith("loom_recall failed: ")
    assert "query" in out


def test_a_non_integer_argument_returns_the_error_to_the_model() -> None:
    (recall,) = loom_tools(_FakeAgent({}), only=["loom_recall"])  # type: ignore[arg-type]
    out = _invoke(recall, {"query": "seat", "top_k": "five"})
    assert out == 'loom_recall failed: tool argument top_k must be an integer, got "five"'


def test_a_server_error_returns_the_error_to_the_model() -> None:
    class _Down(_FakeAgent):
        async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
            raise LoomError("memory store unavailable")

    (remember,) = loom_tools(_Down({}), only=["loom_remember"])  # type: ignore[arg-type]
    out = _invoke(remember, {"memory_key": "seat", "value": "aisle"})
    assert out == "loom_remember failed: memory store unavailable"


def test_max_result_chars_caps_what_the_model_reads() -> None:
    agent = _FakeAgent(
        {"results": [{"memory_key": "k", "content_excerpt": "x" * 500, "similarity": 0.9}]}
    )
    (recall,) = loom_tools(agent, only=["loom_recall"], max_result_chars=100)  # type: ignore[arg-type]
    out = _invoke(recall, {"query": "seat"})
    assert out.endswith("…[truncated]")
    assert len(out) == 100 + len("…[truncated]")


# ---- LoomSession -------------------------------------------------------------


class _Rows:
    """memory.set / list / delete over a dict: (user, session, key) -> value, listed
    by key in pages of `page`."""

    def __init__(self, page: int = 2) -> None:
        self.rows: dict[tuple[str | None, str | None, str], Any] = {}
        self.page = page
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def set(self, key: str, value: Any, memory_type: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("set", {"key": key, "memory_type": memory_type, **kw}))
        self.rows[(kw.get("user_id"), kw.get("session_id"), key)] = json.loads(value)
        return {}

    async def list(self, *, prefix: str, offset: int = 0, **kw: Any) -> dict[str, Any]:
        self.calls.append(("list", {"prefix": prefix, "offset": offset, **kw}))
        keys = sorted(
            k
            for (u, s, k) in self.rows
            if u == kw.get("user_id") and s == kw.get("session_id") and k.startswith(prefix)
        )
        page = keys[offset : offset + self.page]
        rows = [
            {"memory_key": k, "value": self.rows[(kw.get("user_id"), kw.get("session_id"), k)]}
            for k in page
        ]
        return {"entries": rows, "truncated": offset + self.page < len(keys)}

    async def delete(self, key: str, **kw: Any) -> bool:
        self.calls.append(("delete", {"key": key, **kw}))
        return self.rows.pop((kw.get("user_id"), kw.get("session_id"), key), None) is not None


class _Memory:
    def __init__(self, rows: _Rows) -> None:
        self.memory = rows


def _session(rows: _Rows, session_id: str = "s1", **kw: Any) -> Any:
    from loom.integrations.openai_agents import LoomSession

    return LoomSession(_Memory(rows), session_id, **{"user_id": "alice", **kw})  # type: ignore[arg-type]


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text}


def test_a_session_keeps_items_in_order_across_pages_and_repeats() -> None:
    rows = _Rows()
    session = _session(rows)
    items = [_msg("user", "hi"), _msg("assistant", "hello"), _msg("user", "hi")]
    asyncio.run(session.add_items(items))
    asyncio.run(session.add_items([_msg("assistant", "again")]))
    assert asyncio.run(session.get_items()) == [*items, _msg("assistant", "again")]
    assert asyncio.run(session.get_items(limit=2)) == [
        _msg("user", "hi"),
        _msg("assistant", "again"),
    ]
    assert asyncio.run(session.get_items(limit=0)) == []
    sets = [kw for op, kw in rows.calls if op == "set"]
    assert {kw["memory_type"] for kw in sets} == {"episodic"}
    assert {(kw["user_id"], kw["session_id"]) for kw in sets} == {("alice", session._loom_session)}


def test_pop_item_removes_and_returns_the_latest_item() -> None:
    rows = _Rows()
    session = _session(rows)
    asyncio.run(session.add_items([_msg("user", "a"), _msg("assistant", "b")]))
    assert asyncio.run(session.pop_item()) == _msg("assistant", "b")
    assert asyncio.run(session.get_items()) == [_msg("user", "a")]
    assert asyncio.run(session.pop_item()) == _msg("user", "a")
    assert asyncio.run(session.pop_item()) is None


def test_clear_session_deletes_only_this_sessions_items() -> None:
    rows = _Rows()
    mine, other, bobs = _session(rows), _session(rows, "s2"), _session(rows, user_id="bob")
    for session in (mine, other, bobs):
        asyncio.run(session.add_items([_msg("user", "x"), _msg("user", "y"), _msg("user", "z")]))
    asyncio.run(mine.clear_session())
    assert asyncio.run(mine.get_items()) == []
    assert len(asyncio.run(other.get_items())) == len(asyncio.run(bobs.get_items())) == 3


def test_session_settings_limit_is_the_default_limit() -> None:
    from agents.memory import SessionSettings

    rows = _Rows()
    session = _session(rows, session_settings=SessionSettings(limit=1))
    asyncio.run(session.add_items([_msg("user", "a"), _msg("user", "b")]))
    assert asyncio.run(session.get_items()) == [_msg("user", "b")]
    assert len(asyncio.run(session.get_items(limit=5))) == 2


def test_the_loom_session_is_per_user_and_unambiguous() -> None:
    def loom_session(user: str | None, session_id: str) -> str:
        return str(_session(_Rows(), session_id, user_id=user)._loom_session)

    assert loom_session("alice", "s1") != loom_session("bob", "s1")
    assert loom_session("a/b", "c") != loom_session("a", "b/c")
    assert loom_session(None, "s1").startswith("oai-")
    # The TypeScript LoomSession names the same: sha256 of the compact JSON pair.
    assert loom_session("alice", "s1") == "oai-65872b142b2cfb23bddc"


def test_a_session_refuses_a_missing_id_a_user_naming_none_and_an_empty_prefix() -> None:
    with pytest.raises(ValueError, match="session_id"):
        _session(_Rows(), "")
    for user in ("", "-"):
        with pytest.raises(ValueError, match="names none"):
            _session(_Rows(), user_id=user)
    with pytest.raises(ValueError, match="key_prefix"):
        _session(_Rows(), key_prefix="/")


def test_a_runner_reads_and_extends_the_history_through_the_session() -> None:
    from agents import Agent as OpenAIAgent
    from agents import Runner
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.usage import Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    seen: list[list[Any]] = []

    class _Echo(Model):
        async def get_response(
            self, system_instructions: Any, input: Any, *a: Any, **kw: Any
        ) -> ModelResponse:
            seen.append(list(input) if isinstance(input, list) else [input])
            text = ResponseOutputText(text=f"reply {len(seen)}", type="output_text", annotations=[])
            message = ResponseOutputMessage(
                id=f"m{len(seen)}",
                content=[text],
                role="assistant",
                status="completed",
                type="message",
            )
            return ModelResponse(output=[message], usage=Usage(), response_id=None)

        def stream_response(self, *a: Any, **kw: Any) -> Any:
            raise NotImplementedError

    rows = _Rows(page=100)
    session = _session(rows)
    assistant = OpenAIAgent(name="assistant", model=_Echo())
    asyncio.run(Runner.run(assistant, "first", session=session))
    asyncio.run(Runner.run(assistant, "second", session=session))
    second_input = seen[1]
    assert [i.get("content") if isinstance(i, dict) else None for i in second_input][0] == "first"
    history = asyncio.run(session.get_items())
    assert [h.get("role") for h in history] == ["user", "assistant", "user", "assistant"]
