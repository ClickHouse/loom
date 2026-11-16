"""Loom as a Microsoft Agent Framework context provider.

``LoomContextProvider`` implements ``agent_framework.ContextProvider``, the
provider an ``Agent`` takes in ``context_providers=[...]``. Before each run it
adds the Loom memories that match the user's input to the run's instructions;
after the run it sends the turn's text to Loom, which extracts the facts::

    from agent_framework import Agent
    from loom import Agent as LoomAgent
    from loom.integrations.agent_framework import LoomContextProvider

    async with LoomAgent(org="acme", namespace="support") as loom_agent:
        agent = Agent(
            client=chat_client,
            instructions="You are a helpful assistant.",
            context_providers=[LoomContextProvider(loom_agent, user_id="alice")],
        )
        session = agent.create_session()
        await agent.run("I prefer aisle seats", session=session)

The agent keeps its own conversation history: with no history provider
configured it adds its in-memory one, and Loom holds the memories across
sessions. Install with ``pip install 'clickhouse-loom[agent-framework]'``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

try:
    from agent_framework import AgentSession, ContextProvider, Message, SessionContext
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.agent_framework needs agent-framework-core: "
        "pip install 'clickhouse-loom[agent-framework]'"
    ) from e

from loom.sync import SyncAgent

if TYPE_CHECKING:
    from loom.agent import Agent

# The server's cap on the messages of one memory.set_from_messages call.
_MAX_MESSAGES = 400


class LoomContextProvider(ContextProvider):
    """Agent Framework ``ContextProvider`` over a Loom ``Agent``.

    ``before_run`` runs ``memory.search`` with the text of the run's user
    messages and adds the matches as instructions; ``after_run`` sends the text
    of the run's input and response messages to ``memory.set_from_messages``.
    The mapping is in ``docs/features/integrations/frameworks.md``
    (Microsoft Agent Framework). The framework awaits its providers, so the
    provider takes a ``loom.Agent``.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        user_id: str | None = None,
        source_id: str = "loom",
        top_k: int = 5,
        honour_abstain: bool = True,
        defer: bool = True,
    ) -> None:
        if isinstance(agent, SyncAgent):
            raise TypeError(
                "LoomContextProvider needs a loom.Agent: Agent Framework awaits its providers"
            )
        if not 1 <= top_k <= 500:
            raise ValueError("top_k must be between 1 and 500, memory.search's bounds")
        if user_id is not None and user_id.strip() in ("", "-"):
            raise ValueError(f"user_id names a Loom user or is None; {user_id!r} names none")
        super().__init__(source_id)
        self._agent = agent
        self._user_id = user_id
        self._top_k = top_k
        self._honour_abstain = honour_abstain
        self._defer = defer

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        query = " ".join(m.text for m in context.input_messages if m.role == "user" and m.text)
        if not query.strip():
            return
        result = await self._agent.memory.search(
            query,
            top_k=self._top_k,
            user_id=self._user_id,
            # Read the namespace this provider writes to, whatever the deployment's default.
            namespace_scope="this",
            # Pinned so every top_k gets full bodies and no per-hit explanation
            # (the server's defaults switch at 15).
            bodies="full",
            explain=False,
        )
        hits = list(result)
        if self._honour_abstain:
            if result.abstain:
                return
            # A response that does not abstain can still carry rows below the floor.
            hits = [hit for hit in hits if not hit.below_threshold]
        # Derived rows may come back above the top_k slice: cut after filtering.
        lines = [f"{i}. {hit.content_excerpt}" for i, hit in enumerate(hits[: self._top_k], 1)]
        if lines:
            context.extend_instructions(
                self.source_id, "Relevant memory content from Loom:\n" + "\n".join(lines)
            )

    async def after_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        response = context.response
        turn = [*context.input_messages, *(response.messages if response else [])]
        messages = _texts(turn)
        loom_session = _loom_session(self._user_id or "-", context.session_id or session.session_id)
        for start in range(0, len(messages), _MAX_MESSAGES):
            await self._agent.memory.set_from_messages(
                messages[start : start + _MAX_MESSAGES],
                user_id=self._user_id,
                session_id=loom_session,
                defer=self._defer,
            )


def _texts(messages: Sequence[Message]) -> list[dict[str, object]]:
    """The user and assistant text of `messages`; tool calls and results are left out."""
    out: list[dict[str, object]] = []
    for message in messages:
        if message.role in ("user", "assistant") and message.text.strip():
            out.append({"role": message.role, "content": message.text})
    return out


def _loom_session(user_id: str, session_id: str) -> str:
    """The Loom session of an Agent Framework session: Loom files a turn's verbatim
    chunks by session alone, and a caller may choose its session ids."""
    pair = json.dumps([user_id, session_id])
    return f"maf-{hashlib.sha256(pair.encode()).hexdigest()[:20]}"
