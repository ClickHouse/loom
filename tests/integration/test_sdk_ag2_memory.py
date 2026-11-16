"""loom.integrations.ag2 against a running Loom, through AG2 runs with AG2's
scripted model: the model's loom_remember stores a fact, and in a later run
LoomRecallPolicy puts it in the prompt the model sees."""

from __future__ import annotations

import json

import pytest

from tests.integration._eventually import eventually

# An SDK extra, outside the server's environment: install ag2 to run these.
pytest.importorskip("ag2")

from ag2 import Agent as AG2Agent
from ag2 import Middleware
from ag2.assembly import AssemblerMiddleware
from ag2.events import ToolCallEvent
from ag2.testing import TestConfig, TrackingConfig
from loom.integrations.ag2 import LoomRecallPolicy, loom_tools


async def test_remember_in_one_run_and_the_policy_recalls_in_the_next(agent) -> None:
    remember = ToolCallEvent(
        name="loom_remember",
        arguments=json.dumps(
            {"memory_key": "seat_preference", "value": "Alice prefers aisle seats on long flights"}
        ),
    )
    writer = AG2Agent(
        "assistant",
        config=TestConfig(remember, "Noted."),
        tools=loom_tools(agent, user_id="alice", only=["loom_remember"]),
    )
    assert (
        await (await writer.ask("I always take an aisle seat on long flights")).content()
        == "Noted."
    )

    recall = LoomRecallPolicy(agent, user_id="alice", honour_abstain=False)

    async def recalled() -> bool:
        config = TrackingConfig(TestConfig("An aisle seat."))
        reader = AG2Agent(
            "assistant",
            config=config,
            middleware=[Middleware(AssemblerMiddleware, policies=[recall])],
        )
        await reader.ask("Which seat does Alice like on long flights?")
        # No memory yet: the policy adds no block, and the agent has no prompt of its own.
        return any("aisle" in part for part in config.calls[0].prompt)

    # The write is searchable once its embedding lands.
    await eventually(recalled, description="the aisle memory in the prompt")
