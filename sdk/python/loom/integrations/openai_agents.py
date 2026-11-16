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
import re
import secrets
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

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
# A key this session wrote: the prefix, 20 digits of nanoseconds, 8 hex.
_ITEM_KEY = re.compile(r"\d{20}-[0-9a-f]{8}")
_last_stamp = 0  # the last item key's nanoseconds this process wrote


class LoomSession:
    """The OpenAI Agents SDK's ``Session`` over a Loom ``Agent``: the conversation
    history of one session, as episodic memories of the Loom agent and the user.

    Each item is one ``episodic`` memory (not embedded, so never merged with or
    judged against another row) under ``<key_prefix>/<nanoseconds>-<random>``; reads
    sort the items by key, the order they were added. Every process whose Loom agent
    has the same name reads the same Python history for the same session id and user.
    TypeScript sessions have a separate history because their item formats differ. The mapping is in
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
        # Users and SDK languages have separate histories: their native item formats differ.
        pair = json.dumps([user_id or "-", session_id], separators=(",", ":"), ensure_ascii=False)
        self._loom_session = f"oai-py-{hashlib.sha256(pair.encode()).hexdigest()[:20]}"

    def _scope(self) -> dict[str, Any]:
        return {"user_id": self._user_id, "session_id": self._loom_session}

    async def _latest(self, limit: int | None) -> list[dict[str, Any]]:
        """The session's latest `limit` item rows (all when None), oldest first.

        memory.list serves the newest second first and orders by key within a second, so
        a read for `limit` rows stops once it holds `limit` and a row of an older second;
        the rows are then sorted by key, the order the items were added."""
        if limit is not None and limit <= 0:
            return []
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._agent.memory.list(
                prefix=self._prefix,
                memory_type="episodic",
                limit=_PAGE,
                offset=offset,
                fields=["memory_key", "value", "value_truncated", "created_at"],
                **self._scope(),
            )
            offset += len(page["entries"])
            rows += [
                r
                for r in page["entries"]
                if _ITEM_KEY.fullmatch(str(r["memory_key"])[len(self._prefix) :])
            ]
            if not page.get("truncated"):
                break
            if limit is not None and len(rows) > limit:
                if str(rows[-1]["created_at"]) < str(rows[limit - 1]["created_at"]):
                    break
        rows.sort(key=lambda r: str(r["memory_key"]))
        return rows if limit is None else rows[-limit:] if limit > 0 else []

    async def _value(self, row: dict[str, Any]) -> TResponseInputItem:
        """A row's item: the listed value, or the whole one by key when the list cut it
        (or, for a row Loom labels sensitive, sent none)."""
        if row.get("value_truncated") or row.get("value") is None:
            full = await self._agent.memory.get(str(row["memory_key"]), **self._scope())
            return _item(None if full is None else full.get("value"))
        return _item(row["value"])

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        rows = await self._latest(resolve_session_limit(limit, self.session_settings))
        return [await self._value(row) for row in rows]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        global _last_stamp
        for item in items:
            # The clock or one past this process's last stamp, never less: keys keep order.
            _last_stamp = max(time.time_ns(), _last_stamp + 1)
            # One memory.set per item: a batch folds a value identical to a live row.
            await self._agent.memory.set(
                f"{self._prefix}{_last_stamp:020d}-{secrets.token_hex(4)}",
                json.dumps(item),
                memory_type="episodic",
                **self._scope(),
            )

    async def pop_item(self) -> TResponseInputItem | None:
        rows = await self._latest(1)
        if not rows:
            return None
        item = await self._value(rows[-1])
        await self._agent.memory.delete(str(rows[-1]["memory_key"]), **self._scope())
        return item

    async def clear_session(self) -> None:
        for row in await self._latest(None):
            await self._agent.memory.delete(str(row["memory_key"]), **self._scope())


def _item(value: Any) -> TResponseInputItem:
    """A stored item: the JSON text it was written as, or the value Loom parsed from it."""
    parsed = json.loads(value) if isinstance(value, str) else value
    return cast(TResponseInputItem, parsed)
