"""loom.integrations.ag2 against a running Loom: the tools' loom_remember
stores a fact, and LoomRecallPolicy adds it to the prompt for a question
about it."""

from __future__ import annotations

import json

import pytest

from tests.integration._eventually import eventually

# An SDK extra, outside the server's environment: install ag2 to run these.
pytest.importorskip("ag2")

from ag2.events import ModelRequest
from ag2.events.input_events import TextInput
from loom.integrations.ag2 import LoomRecallPolicy, loom_tools


async def test_remember_then_the_policy_recalls(agent) -> None:
    (remember,) = loom_tools(agent, user_id="alice", only=["loom_remember"])
    stored = json.loads(
        await remember.model.call(
            memory_key="seat_preference", value="Alice prefers aisle seats on long flights"
        )
    )
    assert stored["stored"] is True
    policy = LoomRecallPolicy(agent, user_id="alice", honour_abstain=False)
    asked = [ModelRequest([TextInput("Which seat does Alice like on long flights?")])]

    async def recalled() -> bool:
        prompts, _ = await policy.apply([], asked, None)  # type: ignore[arg-type]
        return bool(prompts) and "aisle" in prompts[-1]

    # The write is searchable once its embedding lands.
    await eventually(recalled, description="the aisle memory in the prompt")
