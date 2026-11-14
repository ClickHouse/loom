"""Loom as AG2 memory: a recall policy and Loom's agent verbs as tools.

An AG2 1.3 agent gets context through assembly policies, which run before
each model call, and acts through tools. ``LoomRecallPolicy`` is a policy: it
searches Loom with the latest user message and adds the matching memories to
the prompt. ``loom_tools`` returns Loom's agent verbs (``loom_remember``,
``loom_recall``, ``loom_ask``, ``loom_offload``, ``loom_fetch``,
``loom_note``) as AG2 tools, so the model keeps and reads long-term memory in
Loom during a run::

    from ag2 import Agent as AG2Agent, Middleware
    from ag2.assembly import AssemblerMiddleware
    from loom import Agent
    from loom.integrations.ag2 import LoomRecallPolicy, loom_tools

    async with Agent(org="acme", namespace="support") as loom_agent:
        recall = LoomRecallPolicy(loom_agent, user_id="alice")
        assistant = AG2Agent(
            "assistant",
            config=config,
            tools=loom_tools(loom_agent, user_id="alice"),
            middleware=[Middleware(AssemblerMiddleware, policies=[recall])],
        )
        reply = await assistant.ask("Book my usual seat to Lisbon")

Identity and isolation come from the Loom agent. Install with
``pip install 'clickhouse-loom[ag2]'``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

try:
    from ag2.events import (
        BaseEvent,
        ModelRequest,
        TextInput,
        ToolCallEvent,
        ToolErrorEvent,
        ToolResult,
    )
    from ag2.middleware import ToolExecution, ToolResultType
    from ag2.tools import tool
    from ag2.tools.final import FunctionTool
except ImportError as e:  # the framework is an optional extra
    raise ImportError("loom.integrations.ag2 needs ag2: pip install 'clickhouse-loom[ag2]'") from e

from loom.errors import LoomError
from loom.sync import SyncAgent
from loom.tools import AgentTools

if TYPE_CHECKING:
    from ag2.context import ConversationContext

    from loom.agent import Agent


class LoomRecallPolicy:
    """An AG2 assembly policy that adds Loom's memories to the prompt.

    Before each model call it runs ``memory.search`` of the namespace it is
    bound to with the latest user message, and adds the matches as one prompt
    block. With ``honour_abstain`` it leaves out memories below Loom's floor
    and adds nothing when Loom abstains. It only reads: memories are written
    through ``loom_tools`` (``loom_remember``) or the SDK. Put it before a
    trimming policy (``SlidingWindowPolicy``, ``TokenBudgetPolicy``), which can
    cut the user's request out of the events it reads. Those policies trim
    events, not the prompt, so ``top_k`` is what bounds the memory block.
    """

    name = "loom_memory"

    def __init__(
        self,
        agent: Agent,
        *,
        user_id: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 5,
        honour_abstain: bool = True,
    ) -> None:
        if isinstance(agent, SyncAgent):
            raise TypeError("LoomRecallPolicy needs a loom.Agent: it awaits the agent's search")
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self._agent = agent
        self._user_id = user_id
        self._tags = list(tags or [])
        self._top_k = min(top_k, 500)  # memory.search's bound
        self._honour_abstain = honour_abstain

    async def apply(
        self,
        prompts: list[str],
        events: list[BaseEvent],
        context: ConversationContext,
    ) -> tuple[list[str], list[BaseEvent]]:
        query = _latest_user_text(events)
        if not query:
            return prompts, events
        result = await self._agent.memory.search(
            query,
            top_k=self._top_k,
            tags=self._tags or None,
            user_id=self._user_id,
            # Read the namespace this policy is bound to, whatever the deployment's default.
            namespace_scope="this",
            # Unpinned, a large top_k serves excerpts (above 15 by default)
            # and drops each hit's explanation.
            bodies="full",
            explain=False,
        )
        hits = list(result)
        if self._honour_abstain:
            if result.abstain:
                return prompts, events
            # A response that does not abstain can still carry rows below the floor.
            hits = [hit for hit in hits if not hit.below_threshold]
        # Derived rows may come back above the top_k slice: cut after filtering.
        lines = [f"- {hit.content_excerpt}" for hit in hits[: self._top_k] if hit.content_excerpt]
        if not lines:
            return prompts, events
        return [*prompts, "## Relevant memories from Loom\n\n" + "\n".join(lines)], events


def loom_tools(
    agent: Agent,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    only: Iterable[str] | None = None,
    max_result_chars: int = 8_000,
) -> list[FunctionTool]:
    """Loom's agent verbs as AG2 tools, bound to one scope.

    ``only`` keeps a subset by name, e.g. ``["loom_remember", "loom_recall"]``;
    ``max_result_chars`` caps the string each call returns to the model
    (``loom_recall`` leaves out its lowest-ranked memories whole to fit, counted
    in ``omitted``). A call Loom refuses is a tool error whose text is
    ``<tool> failed: <message>``, in place of the traceback AG2 sends for an
    exception; the run goes on.
    """
    if isinstance(agent, SyncAgent):
        raise TypeError("loom_tools needs a loom.Agent: its tools await the agent's calls")
    tools = AgentTools(
        agent,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        only=only,
        max_result_chars=max_result_chars,
    )
    return [
        tool(
            _runner(tools, spec["name"]),
            name=spec["name"],
            description=spec["description"],
            schema=spec["parameters"],
            middleware=[_loom_error_text],
        )
        for spec in tools.definitions()
    ]


async def _loom_error_text(
    call_next: ToolExecution, event: ToolCallEvent, context: ConversationContext
) -> ToolResultType:
    """A refused call stays a tool error, with Loom's message rather than a traceback."""
    result = await call_next(event, context)
    if isinstance(result, ToolErrorEvent) and isinstance(result.error, LoomError):
        return ToolErrorEvent(
            parent_id=result.parent_id,
            name=result.name,
            error=result.error,
            result=ToolResult(f"{result.name} failed: {result.error}"),
        )
    return result


def _runner(tools: AgentTools, name: str) -> Callable[..., Awaitable[str]]:
    async def run(**arguments: Any) -> str:
        return await tools.execute(name, arguments)

    return run


def _latest_user_text(events: list[BaseEvent]) -> str:
    """The text of the latest request a user sent; a framework inbox message is not one."""
    latest = next(
        (e for e in reversed(events) if isinstance(e, ModelRequest) and e.origin == "user"),
        None,
    )
    if latest is None:
        return ""
    return " ".join(part.content for part in latest.parts if isinstance(part, TextInput)).strip()
