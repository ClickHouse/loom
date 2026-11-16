"""Loom as tools for the OpenAI Agents SDK.

``loom_tools`` returns Loom's agent verbs (``loom_remember``, ``loom_recall``,
``loom_ask``, ``loom_offload``, ``loom_fetch``, ``loom_note``) as the OpenAI
Agents SDK's ``FunctionTool`` objects, so a model keeps and reads long-term
memory in Loom during a run, and ``LoomSession`` keeps a run's conversation
history in Loom as the SDK's ``Session``::

    from agents import Agent as OpenAIAgent, Runner
    from loom import Agent
    from loom.integrations.openai_agents import LoomSession, loom_tools

    async with Agent(org="acme", namespace="support") as loom_agent:
        assistant = OpenAIAgent(
            name="assistant",
            instructions="Recall what you know about the user before answering.",
            tools=loom_tools(loom_agent, user_id="alice"),
        )
        session = LoomSession(loom_agent, "trip-42", user_id="alice")
        result = await Runner.run(assistant, "Book my usual seat to Lisbon", session=session)

The tools are ``loom.tools.AgentTools`` (the same names, schemas and results
as ``agent.tools()``), bound to one user, session or run. Identity and
isolation come from the Loom agent. Install with
``pip install 'clickhouse-loom[openai-agents]'``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

try:
    from agents import FunctionTool, TResponseInputItem
    from agents.memory import SessionSettings
    from agents.memory.session_settings import resolve_session_limit
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.openai_agents needs openai-agents: "
        "pip install 'clickhouse-loom[openai-agents]'"
    ) from e

from loom.errors import LoomError
from loom.tools import AgentTools

if TYPE_CHECKING:
    from loom.agent import Agent


def loom_tools(
    agent: Agent,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    only: Iterable[str] | None = None,
    max_result_chars: int = 8_000,
) -> list[FunctionTool]:
    """Loom's agent verbs as ``FunctionTool``s, bound to one scope.

    ``only`` keeps a subset by name, e.g. ``["loom_remember",
    "loom_recall"]``; ``max_result_chars`` caps the string each call returns
    to the model (``loom_recall`` leaves out its lowest-ranked memories whole
    to fit, counted in ``omitted``). A call Loom refuses returns its error
    message to the model instead of failing the run.
    """
    tools = AgentTools(
        agent,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        only=only,
        max_result_chars=max_result_chars,
    )
    return [_function_tool(tools, spec) for spec in tools.definitions()]


def _function_tool(tools: AgentTools, spec: dict[str, Any]) -> FunctionTool:
    name = spec["name"]

    async def invoke(_ctx: Any, arguments: str) -> str:
        try:
            return await tools.execute(name, arguments)
        except LoomError as e:
            # openai-agents fails the whole run on an exception; the model can fix its call instead.
            return f"{name} failed: {e}"

    return FunctionTool(
        name=name,
        description=spec["description"],
        params_json_schema=spec["parameters"],
        on_invoke_tool=invoke,
        # Strict mode requires every property; the optional ones (top_k, memory_type) are not.
        strict_json_schema=False,
    )


_PAGE = 100


class LoomSession:
    """The OpenAI Agents SDK's ``Session`` over a Loom ``Agent``: the conversation
    history of one session, as episodic memories of the user's Loom session.

    Each item is one ``episodic`` memory (not embedded, so never merged with or
    judged against another row) under ``<key_prefix>/<nanoseconds>-<random>``, so
    keys sort in the order the items were added. Every process that opens the same
    session id for the same user reads the same history. The mapping is in
    ``docs/features/integrations/frameworks.md`` (OpenAI Agents SDK).
    """

    def __init__(
        self,
        agent: Agent,
        session_id: str,
        *,
        user_id: str | None = None,
        key_prefix: str = "openai-agents",
        session_settings: SessionSettings | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id names the session")
        if user_id is not None and user_id.strip() in ("", "-"):
            raise ValueError(f"user_id names a Loom user or is None; {user_id!r} names none")
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._agent = agent
        self._user_id = user_id
        self._prefix = key_prefix.strip("/") + "/"
        if self._prefix == "/":
            raise ValueError("key_prefix must not be empty")
        # Loom files a long value's chunks by session alone, and a caller chooses its ids.
        # Compact JSON, as JavaScript writes it, so the TypeScript session names the same.
        pair = json.dumps([user_id or "-", session_id], separators=(",", ":"))
        self._loom_session = f"oai-{hashlib.sha256(pair.encode()).hexdigest()[:20]}"

    async def _entries(self) -> list[dict[str, Any]]:
        """Every item's row, oldest first (keys sort in the order items were added)."""
        rows: list[dict[str, Any]] = []
        while True:
            page = await self._agent.memory.list(
                prefix=self._prefix,
                memory_type="episodic",
                user_id=self._user_id,
                session_id=self._loom_session,
                limit=_PAGE,
                offset=len(rows),
                fields=["memory_key", "value"],
            )
            rows += page["entries"]
            if not page.get("truncated"):
                return rows

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        rows = await self._entries()
        count = resolve_session_limit(limit, self.session_settings)
        if count is not None:
            rows = rows[-count:] if count > 0 else []
        return [_item(row["value"]) for row in rows]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        start = time.time_ns()
        for i, item in enumerate(items):
            # One memory.set per item: a batch folds a value identical to a live row.
            await self._agent.memory.set(
                f"{self._prefix}{start + i:020d}-{secrets.token_hex(4)}",
                json.dumps(item),
                memory_type="episodic",
                user_id=self._user_id,
                session_id=self._loom_session,
            )

    async def pop_item(self) -> TResponseInputItem | None:
        rows = await self._entries()
        if not rows:
            return None
        last = rows[-1]
        await self._agent.memory.delete(
            last["memory_key"], user_id=self._user_id, session_id=self._loom_session
        )
        return _item(last["value"])

    async def clear_session(self) -> None:
        for row in await self._entries():
            await self._agent.memory.delete(
                row["memory_key"], user_id=self._user_id, session_id=self._loom_session
            )


def _item(value: Any) -> TResponseInputItem:
    """A stored item: the JSON text it was written as, or the value Loom parsed from it."""
    parsed = json.loads(value) if isinstance(value, str) else value
    return parsed  # type: ignore[no-any-return]
