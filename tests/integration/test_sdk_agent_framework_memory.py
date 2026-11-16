"""LoomContextProvider against a running Loom: a turn an Agent Framework agent ran
is extracted after the run, and before a later run in a new session the provider adds
it to the model's instructions, for the same user only."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from tests.integration._eventually import eventually

# An SDK extra, outside the server's environment: install agent-framework-core to run these.
pytest.importorskip("agent_framework")

from agent_framework import Agent, BaseChatClient, ChatResponse, Message
from loom.integrations.agent_framework import LoomContextProvider


class _EchoClient(BaseChatClient):  # type: ignore[misc]
    """Answers "Noted." and keeps the instructions of each call."""

    def __init__(self) -> None:
        super().__init__()
        self.instructions: list[str] = []

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool, options: Any, **kw: Any
    ) -> Any:
        self.instructions.append(str((options or {}).get("instructions") or ""))

        async def respond() -> ChatResponse:
            return ChatResponse(messages=[Message(role="assistant", contents=["Noted."])])

        return respond()


async def _turn(loom: Any, user: str, text: str) -> str:
    """One run in a new session; returns the instructions the model was sent."""
    client = _EchoClient()
    provider = LoomContextProvider(loom, user_id=user, honour_abstain=False, defer=False)
    agent = Agent(client=client, context_providers=[provider])
    await agent.run(text, session=agent.create_session())
    return client.instructions[0]


async def test_a_turn_reaches_a_later_runs_instructions(agent) -> None:
    # defer=False: extraction inline, since the test server runs no background workers
    await _turn(agent, "alice", "I fly to Lisbon every month.")

    async def recalled() -> bool:
        found = await agent.memory.search("Lisbon", user_id="alice", namespace_scope="this")
        return any(h.source == "fact" for h in found)

    # A write is searchable once its embedding lands; eventually raises on timeout.
    await eventually(recalled, description="the turn's fact")
    instructions = await _turn(agent, "alice", "Where does Alice fly every month?")
    assert "Relevant memory content from Loom" in instructions
    assert "lisbon" in instructions.lower()
    other = await _turn(agent, "bob", "Where does Alice fly every month?")
    assert "lisbon" not in other.lower()
