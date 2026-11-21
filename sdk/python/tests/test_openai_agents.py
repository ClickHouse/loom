"""loom_tools: Loom's agent verbs as OpenAI Agents SDK FunctionTools over a
fake Loom agent — names and schemas, the scope each call carries, and the
string the model reads back."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext
from loom import Agent
from loom.capabilities import Capabilities
from loom.errors import LoomError
from loom.integrations.openai_agents import LoomSession, loom_tools
from loom.session_protection import AutomaticCaptureSuppressed, SessionProtection
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
    receipt = {
        "version": 2,
        "memory_id": "5ec521fa-3762-4e56-aa7a-5f4c691bb572",
        "memory_key": "seat",
        "content_sha256": "a" * 64,
        "status": "active",
        "conflict_verdict": "clear",
        "noop": False,
    }
    agent = _FakeAgent(receipt)
    (remember,) = loom_tools(agent, only=["loom_remember"])  # type: ignore[arg-type]
    out = _invoke(remember, {"memory_key": "seat", "value": "Alice prefers aisle seats"})
    assert agent.calls[0][:2] == (
        "memory.set",
        {"memory_key": "seat", "value": "Alice prefers aisle seats"},
    )
    assert json.loads(out) == {**receipt, "stored": True, "verdict": "clear"}


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
    assert json.loads(out) == {"memories": [], "omitted": 1}
    assert len(out) <= 100


# ---- LoomSession -------------------------------------------------------------


class _Rows:
    """memory.set / list / get / delete over a dict, listed as the server lists: the
    newest second first, by key within a second, values cut to 2,048 characters."""

    def __init__(self, unserved: int = 0, cap: int = 100) -> None:
        self.rows: dict[tuple[Any, Any, str], tuple[Any, int]] = {}
        self.unserved = unserved
        self.cap = cap
        self.second = 0  # the clock rows are stamped with; a test moves it
        self.hidden: set[str] = set()  # keys Loom labels sensitive: listed without a value
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def set(self, key: str, value: Any, memory_type: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("set", {"key": key, "memory_type": memory_type, **kw}))
        self.rows[(kw.get("user_id"), kw.get("session_id"), key)] = (json.loads(value), self.second)
        return {}

    async def list(self, *, prefix: str, offset: int = 0, **kw: Any) -> dict[str, Any]:
        self.calls.append(("list", {"prefix": prefix, "offset": offset, **kw}))
        mine = sorted(
            (
                (k, v, t)
                for (u, s_, k), (v, t) in self.rows.items()
                if u == kw.get("user_id") and s_ == kw.get("session_id") and k.startswith(prefix)
            ),
            key=lambda r: (-r[2], r[0]),
        )
        scanned = [None] * self.unserved + mine
        limit = min(self.cap, int(kw["limit"]))
        page = scanned[offset : offset + limit]
        entries = []
        for row in page:
            if row is None:
                continue
            key, value, second = row
            text = json.dumps(value)
            cut = len(text) > 2048
            entries.append(
                {
                    "memory_key": key,
                    "value": None if key in self.hidden else (text[:2048] + "…" if cut else value),
                    "value_truncated": cut or key in self.hidden,
                    "created_at": f"2026-01-01T00:00:{second:02d}",
                }
            )
        return {"entries": entries, "truncated": offset + limit < len(scanned)}

    async def get(self, key: str, **kw: Any) -> dict[str, Any] | None:
        self.calls.append(("get", {"key": key, **kw}))
        row = self.rows.get((kw.get("user_id"), kw.get("session_id"), key))
        return None if row is None else {"value": row[0]}

    async def delete(self, key: str, **kw: Any) -> bool:
        self.calls.append(("delete", {"key": key, **kw}))
        return self.rows.pop((kw.get("user_id"), kw.get("session_id"), key), None) is not None


class _Memory:
    def __init__(self, rows: _Rows) -> None:
        self.memory = rows
        self.transport = SimpleNamespace(session_protection=None)

    async def capabilities(self) -> Capabilities:
        return Capabilities.from_response({"ops": [{"name": "memory.append_batch"}]}, None)

    async def call(self, op: str, body: dict[str, Any], **scope: Any) -> dict[str, Any]:
        assert op == "memory.append_batch"
        for entry in body["entries"]:
            await self.memory.set(entry["memory_key"], entry["value"], "episodic", **scope)
        return {}


def _session(rows: _Rows, session_id: str = "s1", **kw: Any) -> Any:
    from loom.integrations.openai_agents import LoomSession

    return LoomSession(_Memory(rows), session_id, **{"user_id": "alice", **kw})  # type: ignore[arg-type]


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text}


def test_a_session_keeps_items_in_order_across_pages_and_repeats() -> None:
    rows = _Rows()
    session = _session(rows)
    items = [
        *[_msg("user", f"m{i}") for i in range(100)],
        _msg("user", "hi"),
        _msg("assistant", "hello"),
        _msg("user", "hi"),
    ]
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


def test_an_empty_truncated_page_advances_past_unserved_projections() -> None:
    rows = _Rows(unserved=100, cap=50)
    session = _session(rows)
    items = [_msg("user", "hi"), _msg("assistant", "hello")]
    asyncio.run(session.add_items(items))
    assert asyncio.run(session.get_items()) == items
    offsets = [kw["offset"] for op, kw in rows.calls if op == "list"]
    assert offsets == sorted(set(offsets))


def test_a_viewer_cap_keeps_every_item_once() -> None:
    rows = _Rows(cap=50)
    session = _session(rows)
    items = [_msg("user", f"m{i}") for i in range(120)]
    asyncio.run(session.add_items(items))
    assert asyncio.run(session.get_items()) == items


def test_pop_item_removes_and_returns_the_latest_item() -> None:
    rows = _Rows()
    session = _session(rows)
    asyncio.run(session.add_items([_msg("user", "a"), _msg("assistant", "b")]))
    assert asyncio.run(session.pop_item()) == _msg("assistant", "b")
    assert asyncio.run(session.get_items()) == [_msg("user", "a")]
    assert asyncio.run(session.pop_item()) == _msg("user", "a")
    assert asyncio.run(session.pop_item()) is None


def test_native_tool_history_does_not_read_typescript_items() -> None:
    rows = _Rows()
    session = _session(rows)
    items = [
        {"type": "function_call", "call_id": "c1", "name": "recall", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "remembered"},
    ]
    asyncio.run(session.add_items(items))
    key = next(iter(rows.rows))[2]
    asyncio.run(
        rows.set(
            key,
            json.dumps({"type": "function_call_result", "callId": "c1", "output": "other"}),
            "episodic",
            user_id="alice",
            session_id=session._loom_session.replace("oai-py-", "oai-ts-"),
        )
    )
    assert asyncio.run(session.get_items()) == items


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
    assert loom_session(None, "s1").startswith("oai-py-")
    assert loom_session("josé", "s1") != loom_session("jose", "s1")
    assert (
        loom_session("josé", "s1") == "oai-py-c10f2e9c4ca65c3f8b98"
    )  # unescaped, as JavaScript writes it
    # TypeScript uses oai-ts- so native item formats never mix.
    assert loom_session("alice", "s1") == "oai-py-65872b142b2cfb23bddc"


def test_a_session_refuses_a_missing_id_a_user_naming_none_and_an_empty_prefix() -> None:
    with pytest.raises(ValueError, match="session_id"):
        _session(_Rows(), "")
    for user in ("", "-", " alice", "alice "):
        with pytest.raises(ValueError, match="user_id"):
            _session(_Rows(), user_id=user)
    with pytest.raises(ValueError, match="key_prefix"):
        _session(_Rows(), key_prefix="/")


def test_history_spanning_seconds_comes_back_in_the_order_it_was_added() -> None:
    rows = _Rows()
    session = _session(rows)
    for second, batch in enumerate([["u1", "a1"], ["u2", "a2"], ["u3", "a3"]]):
        rows.second = second
        asyncio.run(session.add_items([_msg("user", batch[0]), _msg("assistant", batch[1])]))
    contents = [i["content"] for i in asyncio.run(session.get_items())]
    assert contents == ["u1", "a1", "u2", "a2", "u3", "a3"]
    assert [i["content"] for i in asyncio.run(session.get_items(limit=3))] == ["a2", "u3", "a3"]
    assert asyncio.run(session.pop_item()) == _msg("assistant", "a3")


def test_a_latest_read_returns_the_last_item_from_a_single_page() -> None:
    rows = _Rows()
    session = _session(rows)
    for second in range(6):
        rows.second = second
        asyncio.run(session.add_items([_msg("user", f"m{second}")]))
    rows.calls.clear()
    assert [i["content"] for i in asyncio.run(session.get_items(limit=1))] == ["m5"]
    assert len([c for c in rows.calls if c[0] == "list"]) == 1


@pytest.mark.parametrize("method", ["get_items", "pop_item"])
def test_limited_history_uses_the_same_key_order_when_server_times_differ(method: str) -> None:
    rows = _Rows()
    session = _session(rows)
    items = [_msg("user", f"m{i}") for i in range(102)]
    # Server arrival time need not follow keys from different writers' clocks.
    for second, batch in [(2, items[:1]), (1, items[1:101]), (0, items[101:])]:
        rows.second = second
        asyncio.run(session.add_items(batch))
    history = asyncio.run(session.get_items())
    assert history == items
    if method == "get_items":
        assert asyncio.run(session.get_items(limit=1)) == history[-1:]
    else:
        assert asyncio.run(session.pop_item()) == history[-1]
        assert asyncio.run(session.get_items()) == history[:-1]


def test_a_long_or_sensitive_item_is_read_whole_by_key() -> None:
    rows = _Rows()
    session = _session(rows)
    long = _msg("assistant", "x" * 5000)
    asyncio.run(session.add_items([_msg("user", "my key is sk-123"), long]))
    rows.hidden = {
        k for (_, _, k), (v, _) in rows.rows.items() if v["content"].startswith("my key")
    }
    assert asyncio.run(session.get_items()) == [_msg("user", "my key is sk-123"), long]
    assert len([c for c in rows.calls if c[0] == "get"]) == 2


@pytest.mark.parametrize("method", ["get_items", "pop_item"])
def test_a_disappearing_item_fails_instead_of_returning_invalid_history(method: str) -> None:
    class _DisappearingRows(_Rows):
        async def get(self, key: str, **kw: Any) -> None:
            await self.delete(key, **kw)
            return None

    session = _session(_DisappearingRows())
    asyncio.run(session.add_items([_msg("assistant", "x" * 5000)]))
    with pytest.raises(LoomError, match="unavailable"):
        asyncio.run(getattr(session, method)())


def test_concurrent_repeats_use_atomic_batches_with_default_sdk_coalescing() -> None:
    from loom.integrations.openai_agents import LoomSession

    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, body: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        if path == "/v1/ops.capabilities":
            return {"ops": [{"name": "memory.append_batch"}]}
        posts.append((path, dict(body)))
        return {}

    async def append() -> None:
        async with Agent(
            org="o", namespace="n", agent="bot", api_key="k", mentor_brief=False, auto_run=False
        ) as agent:
            assert agent.coalesce_sets
            agent.transport.post = post  # type: ignore[method-assign]
            session = LoomSession(agent, "s1", user_id="alice")
            await asyncio.gather(*(session.add_items([_msg("user", "again")]) for _ in range(4)))
            assert agent.coalesce_sets

    asyncio.run(append())
    assert [path for path, _ in posts] == ["/v1/memory.append_batch"] * 4
    assert all(len(body["entries"]) == 1 for _, body in posts)
    assert len({body["entries"][0]["memory_key"] for _, body in posts}) == 4
    assert {body["entries"][0]["value"] for _, body in posts} == {json.dumps(_msg("user", "again"))}


def test_a_nested_key_prefix_is_not_read_as_this_sessions_items() -> None:
    rows = _Rows()
    outer, inner = _session(rows, key_prefix="a"), _session(rows, key_prefix="a/b")
    asyncio.run(inner.add_items([_msg("user", "inner")]))
    assert asyncio.run(outer.get_items()) == []
    asyncio.run(outer.clear_session())
    assert asyncio.run(inner.get_items()) == [_msg("user", "inner")]


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

    rows = _Rows()
    session = _session(rows)
    assistant = OpenAIAgent(name="assistant", model=_Echo())
    asyncio.run(Runner.run(assistant, "first", session=session))
    asyncio.run(Runner.run(assistant, "second", session=session))
    second_input = seen[1]
    assert [i.get("content") if isinstance(i, dict) else None for i in second_input][0] == "first"
    history = asyncio.run(session.get_items())
    assert [h.get("role") for h in history] == ["user", "assistant", "user", "assistant"]


def _protected_session_agent(
    family: SessionProtection,
    respond: Callable[[httpx.Request], httpx.Response],
) -> Agent:
    agent = Agent(
        org="acme",
        namespace="support",
        agent="bot",
        base_url="http://inert.invalid",
        api_key="test-token",
        session_protection=family,
        pacing=False,
        coalesce_sets=False,
        auto_run=False,
        mentor_brief=False,
        retry={"max_attempts": 1},
    )
    agent.transport._client._transport = httpx.MockTransport(respond)
    agent._caps = Capabilities.from_response({"ops": [{"name": "memory.append_batch"}]}, None)
    return agent


_HISTORY_KEY = "openai-agents/00000000000000000001-12345678"


def _history_page(*, mirrored: bool = False, truncated: bool = False) -> dict[str, Any]:
    return {
        "mirrored": mirrored,
        "truncated": truncated,
        "entries": [{"memory_key": _HISTORY_KEY, "value": _msg("user", "retained history")}],
    }


@pytest.mark.parametrize("method", ["get_items", "add_items"])
@pytest.mark.parametrize("origin", ["bound", "ambient"])
def test_native_history_retains_constructor_family_after_rebinding(
    tmp_path: Path,
    method: str,
    origin: str,
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(
            200, json=_history_page() if request.url.path.endswith("list") else {}
        )

    async def go() -> None:
        agent = _protected_session_agent(original if origin == "bound" else replacement, respond)
        try:
            if origin == "ambient":
                with original.automatic():
                    session = LoomSession(agent, "native-history")
            else:
                session = LoomSession(agent, "native-history")
            agent.transport.session_protection = replacement
            original.mark()
            with pytest.raises(AutomaticCaptureSuppressed):
                if method == "get_items":
                    await session.get_items()
                else:
                    await session.add_items([_msg("assistant", "new automatic item")])
            assert posts == [] and not replacement.protected
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_first_marked_native_history_marks_all_contributors_before_return(tmp_path: Path) -> None:
    contributor = SessionProtection.create(base=tmp_path / "contributor")
    bound = SessionProtection.create(base=tmp_path / "bound")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json=_history_page(mirrored=True))

    async def go() -> None:
        agent = _protected_session_agent(bound, respond)
        try:
            with contributor.automatic():
                session = LoomSession(agent, "native-history")
            assert await session.get_items() == [_msg("user", "retained history")]
            assert contributor.protected and bound.protected
            with pytest.raises(AutomaticCaptureSuppressed):
                await session.add_items([_msg("assistant", "derived reply")])
            assert posts == ["/v1/memory.list"]
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_callback_native_history_contributor_survives_ambient_scope_exit(tmp_path: Path) -> None:
    contributor = SessionProtection.create(base=tmp_path / "contributor")
    bound = SessionProtection.create(base=tmp_path / "bound")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json=_history_page())

    async def go() -> None:
        agent = _protected_session_agent(bound, respond)
        session = LoomSession(agent, "native-history")
        try:
            with contributor.automatic():
                assert await session.get_items() == [_msg("user", "retained history")]
            contributor.mark()
            with pytest.raises(AutomaticCaptureSuppressed):
                await session.add_items([_msg("assistant", "derived reply")])
            assert posts == ["/v1/memory.list"] and not bound.protected
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_marked_native_batch_response_protects_the_whole_callback(tmp_path: Path) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    writes: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops.capabilities":
            return httpx.Response(200, json={"ops": [{"name": "memory.append_batch"}]})
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={"mirrored": True})

    async def go() -> None:
        agent = _protected_session_agent(family, respond)
        try:
            session = LoomSession(agent, "native-history")
            with pytest.raises(AutomaticCaptureSuppressed):
                await session.add_items([_msg("user", "first"), _msg("assistant", "second")])
            assert len(writes) == 1
            assert [json.loads(entry["value"]) for entry in writes[0]["entries"]] == [
                _msg("user", "first"),
                _msg("assistant", "second"),
            ]
            assert family.protected
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_marked_native_history_page_stops_automatic_followup(tmp_path: Path) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(
            200, json=_history_page(mirrored=len(posts) == 1, truncated=len(posts) == 1)
        )

    async def go() -> None:
        agent = _protected_session_agent(family, respond)
        try:
            with pytest.raises(AutomaticCaptureSuppressed):
                await LoomSession(agent, "native-history").get_items()
            assert posts == ["/v1/memory.list"] and family.protected
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_native_history_rechecks_callback_family_after_credentials(tmp_path: Path) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    callback = SessionProtection.create(base=tmp_path / "callback")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json=_history_page())

    async def go() -> None:
        agent = _protected_session_agent(original, respond)
        session = LoomSession(agent, "native-history")
        agent.transport.session_protection = callback

        class RebindingAuth(httpx.Auth):
            async def async_auth_flow(self, request: httpx.Request):
                await asyncio.sleep(0)
                callback.mark()
                agent.transport.session_protection = replacement
                yield request

        agent.transport._client.auth = RebindingAuth()
        try:
            with pytest.raises(AutomaticCaptureSuppressed):
                await session.get_items()
            assert posts == [] and not original.protected and not replacement.protected
        finally:
            await agent.transport.close()

    asyncio.run(go())


@pytest.mark.parametrize("method", ["pop_item", "clear_session"])
def test_explicit_native_history_delete_observes_retained_contributors(
    tmp_path: Path,
    method: str,
) -> None:
    original = SessionProtection.create(base=tmp_path / "original")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        if request.url.path.endswith("list"):
            return httpx.Response(200, json=_history_page(mirrored=True))
        return httpx.Response(200, json={"deleted": True})

    async def go() -> None:
        agent = _protected_session_agent(original, respond)
        session = LoomSession(agent, "native-history")
        agent.transport.session_protection = replacement
        replacement.mark()
        try:
            if method == "pop_item":
                assert await session.pop_item() == _msg("user", "retained history")
            else:
                assert await session.clear_session() is None
            assert original.protected and replacement.protected
            assert posts == ["/v1/memory.list", "/v1/memory.delete"]
        finally:
            await agent.transport.close()

    asyncio.run(go())


# The transport tests keep serialization, discovery and mutation failures separate.
def test_a_later_unencodable_session_item_dispatches_nothing(tmp_path: Path) -> None:
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, json={})

    async def go() -> None:
        family = SessionProtection.create(base=tmp_path / "family")
        agent = _protected_session_agent(family, respond)
        try:
            with pytest.raises(TypeError):
                await LoomSession(agent, "s").add_items(
                    [_msg("user", "first"), {"role": "user", "content": object()}]  # type: ignore[list-item]
                )
            assert posts == []
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_an_empty_session_addition_needs_no_discovery_or_mutation() -> None:
    class EmptyOnly(_Memory):
        async def capabilities(self) -> Capabilities:
            raise AssertionError("empty additions need no discovery")

        async def call(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            raise AssertionError("empty additions need no mutation")

    asyncio.run(LoomSession(EmptyOnly(_Rows()), "s").add_items([]))  # type: ignore[arg-type]


@pytest.mark.parametrize("failure", ["discovery", "absent", "old_replica", "refused", "committed"])
def test_session_batch_failures_never_fall_back_or_change_keys(
    tmp_path: Path, failure: str
) -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/v1/ops.capabilities":
            if failure == "discovery":
                return httpx.Response(503, json={"detail": {"code": "BACKEND_UNAVAILABLE"}})
            return httpx.Response(200, json={"ops": []})
        assert request.url.path == "/v1/memory.append_batch"
        if failure == "old_replica":
            return httpx.Response(404, json={"detail": "Not Found"})
        code = "PUBLICATION_PENDING" if failure == "committed" else "VALIDATION_FAILED"
        return httpx.Response(
            503 if failure == "committed" else 422,
            json={
                "detail": {
                    "code": code,
                    "message": "batch refused",
                    "committed": failure == "committed",
                }
            },
        )

    async def go() -> None:
        family = SessionProtection.create(base=tmp_path / "family")
        agent = _protected_session_agent(family, respond)
        if failure in {"discovery", "absent"}:
            agent._caps = None
        try:
            with pytest.raises(LoomError) as raised:
                await LoomSession(agent, "s").add_items(
                    [_msg("user", "repeated"), _msg("user", "repeated")]
                )
            assert not family.protected
            assert len(posts) == 1
            if failure in {"discovery", "absent"}:
                assert posts[0][0] == "/v1/ops.capabilities"
            else:
                entries = posts[0][1]["entries"]
                assert len(entries) == 2
                assert entries[0]["memory_key"] != entries[1]["memory_key"]
                assert entries[0]["value"] == entries[1]["value"]
                if failure == "committed":
                    assert raised.value.code == "PUBLICATION_PENDING"
                    assert raised.value.detail["committed"] is True
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_a_marked_discovery_reply_stops_before_sending_history(tmp_path: Path) -> None:
    posts: list[str] = []
    family = SessionProtection.create(base=tmp_path / "family")

    def respond(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "mirrored": True,
                "ops": [{"name": "memory.append_batch"}],
            },
        )

    async def go() -> None:
        agent = _protected_session_agent(family, respond)
        agent._caps = None
        try:
            with pytest.raises(AutomaticCaptureSuppressed):
                await LoomSession(agent, "s").add_items([_msg("user", "private")])
            assert posts == ["/v1/ops.capabilities"] and family.protected
        finally:
            await agent.transport.close()

    asyncio.run(go())


def test_an_oversized_session_addition_is_not_split_or_dispatched() -> None:
    class TooLarge(_Memory):
        async def capabilities(self) -> Capabilities:
            raise AssertionError("oversized additions need no discovery")

    session = LoomSession(TooLarge(_Rows()), "s")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at most 1,000"):
        asyncio.run(session.add_items([_msg("user", "again")] * 1001))
