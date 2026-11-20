"""LoomMemoryService against a running Loom: a turn an ADK Runner saves is
extracted, and preload_memory puts it in a later session's prompt for the same
user only; a memory, extracted or explicit, is deleted by the id a search
returned."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from tests.integration._eventually import eventually
from tests.integration._resources import minted_identity

# An SDK extra, outside the server's environment: install google-adk to run these.
pytest.importorskip("google.adk")

from google.adk.agents import LlmAgent
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import preload_memory
from google.genai import types
from loom import Agent
from loom.integrations.google_adk import LoomMemoryService
from pydantic import Field


@pytest.fixture
def adk_identity(mint_test_token) -> dict[str, str]:
    return minted_identity(mint_test_token, "google-adk")


@pytest_asyncio.fixture
async def agent(
    control_plane_url: str, adk_identity: dict[str, str]
) -> AsyncGenerator[Agent, None]:
    async with Agent(
        org=adk_identity["org"],
        namespace=adk_identity["namespace"],
        agent="adk-bot",
        base_url=control_plane_url,
        api_key=adk_identity["token"],
    ) as client:
        yield client


class _EchoLlm(BaseLlm):
    """Answers "Noted." to every call and keeps the requests."""

    model: str = "echo"
    requests: list[LlmRequest] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Noted.")]))


async def _save_turn(ctx: Any) -> None:
    turn = [e for e in ctx.session.events if e.invocation_id == ctx.invocation_id]
    await ctx.add_events_to_memory(events=turn)


async def _turn(service: LoomMemoryService, user: str, text: str) -> str:
    """One turn in a new session of the app "travel"; returns the model's prompt text."""
    llm = _EchoLlm()
    sessions = InMemorySessionService()
    runner = Runner(
        app_name="travel",
        agent=LlmAgent(
            name="assistant", model=llm, tools=[preload_memory], after_agent_callback=_save_turn
        ),
        session_service=sessions,
        memory_service=service,
    )
    session = await sessions.create_session(app_name="travel", user_id=user)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    async for _ in runner.run_async(user_id=user, session_id=session.id, new_message=message):
        pass
    return " ".join(p.text or "" for c in llm.requests[0].contents for p in c.parts or [])


async def test_a_saved_turn_reaches_a_later_prompt(agent) -> None:
    # defer=False: extraction inline, since the test server runs no background workers
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False, defer=False)
    await _turn(service, "alice", "I fly to Lisbon every month.")

    async def saved() -> bool:
        found = await service.search_memory(app_name="travel", user_id="alice", query="Lisbon")
        return any(m.custom_metadata["source"] == "fact" for m in found.memories)

    # A write is searchable once its embedding lands; eventually raises on timeout.
    await eventually(saved, description="the saved turn's fact")
    prompt = await _turn(service, "alice", "Where does Alice fly every month?")
    assert "lisbon" in prompt.lower()
    found = await service.search_memory(app_name="travel", user_id="bob", query="Lisbon")
    assert not found.memories
    found = await service.search_memory(app_name="travel", user_id="alice", query="Lisbon")
    facts = {m.id for m in found.memories if m.custom_metadata["source"] == "fact"}
    assert facts
    for memory_id in facts:
        await service.delete_memory(app_name="travel", user_id="alice", memory_id=memory_id)
    found = await service.search_memory(app_name="travel", user_id="alice", query="Lisbon")
    # The deleted facts do not come back.
    assert not {m.id for m in found.memories} & facts


async def test_add_memory_then_delete_by_id(agent, adk_identity: dict[str, str]) -> None:
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False)
    entry = MemoryEntry(
        content=types.Content(parts=[types.Part(text="Alice prefers aisle seats")]),
        timestamp="2026-03-01T09:00:00+00:00",
    )
    await service.add_memory(app_name="travel", user_id="alice", memories=[entry])

    async def ids(user: str) -> list[str | None]:
        found = await service.search_memory(app_name="travel", user_id=user, query="aisle seat")
        return [m.id for m in found.memories]

    await eventually(lambda: ids("alice"), description="the memory found")
    (memory_id,) = await ids("alice")
    assert memory_id is not None
    key, _session, _writer, _version = json.loads(memory_id)
    row = await agent.memory.get(key, user_id="alice")
    assert row is not None
    fields = ("value", "agent", "user_id", "session_id", "memory_type", "source_class", "tags")
    assert [row[f] for f in fields] == [
        "Alice prefers aisle seats",
        agent.agent,
        "alice",
        "-",
        "semantic",
        "asserted",
        [],
    ]
    assert row["valid_at"].startswith("2026-03-01")
    # The server stamps the actor from the token: its subject, as `<kind>:<subject>`.
    assert row["actor"].endswith(f":{adk_identity['subject']}")
    await service.delete_memory(app_name="travel", user_id="bob", memory_id=memory_id)
    assert await ids("alice") == [memory_id]  # another user's delete reaches nothing
    await service.delete_memory(app_name="travel", user_id="alice", memory_id=memory_id)
    assert await ids("alice") == []


async def test_two_users_saving_the_same_text_each_find_their_own(agent) -> None:
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False)
    entry = MemoryEntry(content=types.Content(parts=[types.Part(text="Prefers window seats")]))

    async def ids(user: str) -> list[str | None]:
        found = await service.search_memory(app_name="travel", user_id=user, query="window seat")
        return [m.id for m in found.memories]

    await service.add_memory(app_name="travel", user_id="carol", memories=[entry])

    async def carols_vector() -> list[str]:
        hits = await agent.memory.search("window seats", memory_type="semantic", top_k=5)
        return [h.memory_key for h in hits]

    # Loom's write-time check reads vectors: dave saves once carol's is searchable.
    await eventually(carols_vector, description="carol's vector")
    await service.add_memory(app_name="travel", user_id="dave", memories=[entry])
    carol = await ids("carol")
    dave = await eventually(lambda: ids("dave"), description="dave's memory")
    assert len(carol) == len(dave) == 1 and carol != dave


async def test_each_turn_saved_in_one_session_keeps_its_passage(agent) -> None:
    """The documented callback saves each turn on its own under one session; a search
    for words only the first turn said still finds that turn's verbatim passage."""
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False, defer=False)
    llm = _EchoLlm()
    sessions = InMemorySessionService()
    runner = Runner(
        app_name="travel",
        agent=LlmAgent(name="assistant", model=llm, after_agent_callback=_save_turn),
        session_service=sessions,
        memory_service=service,
    )
    session = await sessions.create_session(app_name="travel", user_id="alice")
    for text in (
        "My cat Zanzibar loves sardines from the harbour market.",
        "Tomorrow I am repainting the garage door bright teal.",
    ):
        message = types.Content(role="user", parts=[types.Part(text=text)])
        async for _ in runner.run_async(
            user_id="alice", session_id=session.id, new_message=message
        ):
            pass

    async def passage(query: str, words: str) -> bool:
        found = await service.search_memory(app_name="travel", user_id="alice", query=query)
        return any(
            m.custom_metadata["source"] == "chunk" and words in (m.content.parts[0].text or "")
            for m in found.memories
            if m.content and m.content.parts
        )

    await eventually(lambda: passage("Zanzibar sardines harbour", "Zanzibar"), description="turn 1")
    assert await passage("garage door teal", "garage door")


async def test_a_users_delete_reaches_their_fact_when_another_user_holds_its_key(
    agent, require_fake_llm
) -> None:
    """Extracted keys are concept names two users share; the entry id carries the
    session, so Alice's delete reaches her row and leaves Bob's."""
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False, defer=False)
    for user in ("alice", "bob"):
        await _turn(service, user, "I fly to Lisbon every month.")

    async def facts(user: str) -> set[str | None]:
        found = await service.search_memory(app_name="travel", user_id=user, query="Lisbon")
        return {m.id for m in found.memories if m.custom_metadata["source"] == "fact"}

    async def both() -> tuple[set[str | None], set[str | None]] | None:
        alices, bobs = await facts("alice"), await facts("bob")
        keys = {json.loads(i)[0] for i in alices} & {json.loads(i)[0] for i in bobs}
        return (alices, bobs) if keys else None  # wait until the two share a key

    alices, bobs = await eventually(both, description="the users' facts sharing a key")
    for memory_id in alices:
        await service.delete_memory(app_name="travel", user_id="alice", memory_id=memory_id)
    assert not await facts("alice") & alices
    assert bobs <= await facts("bob")


async def test_a_delete_reaches_the_key_in_each_session_it_is_served_from(agent) -> None:
    """A key the user holds in two sessions is served once per session, each under its
    own id, so deleting what a search returns leaves the key unserved."""
    for session, city in (("adk-a", "Lisbon"), ("adk-b", "Porto")):
        await agent.memory.set(
            "user_home_city", f"Alice lives in {city}", user_id="alice", session_id=session
        )
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False)

    async def facts() -> set[str | None]:
        found = await service.search_memory(app_name="travel", user_id="alice", query="Alice lives")
        return {m.id for m in found.memories if m.custom_metadata["source"] == "fact"}

    async def two() -> set[str | None] | None:
        ids = await facts()
        return ids if len(ids) == 2 else None

    for memory_id in await eventually(two, description="both sessions' rows"):
        await service.delete_memory(app_name="travel", user_id="alice", memory_id=memory_id)
    assert await facts() == set()


async def test_knowledge_is_shared_by_app_without_a_fact_deletion_id(agent) -> None:
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False)
    await agent.knowledge.upsert(
        "Aisle seats are described in the travel guide.", source_uri="guide"
    )

    async def documents(user: str):
        result = await service.search_memory(app_name="travel", user_id=user, query="aisle seats")
        return [m for m in result.memories if m.custom_metadata["source"] == "knowledge"]

    await eventually(lambda: documents("alice"), description="the app knowledge document")
    for user in ("alice", "bob"):
        found = await documents(user)
        assert found and found[0].id is None
        assert found[0].custom_metadata["source_uri"] == "guide"


async def test_deleting_one_writers_search_hit_preserves_another_writer(agent) -> None:
    for writer, text in (("writer-a", "Prefers aisle seats"), ("writer-b", "Prefers window seats")):
        await agent.memory.set("seat", text, agent_id=writer, user_id="alice", session_id="shared")
    service = LoomMemoryService(agent, app_name="travel", honour_abstain=False)

    async def both():
        found = await service.search_memory(app_name="travel", user_id="alice", query="seat")
        facts = [m for m in found.memories if m.custom_metadata["source"] == "fact"]
        return facts if len(facts) == 2 else None

    facts = await eventually(both, description="two writers' same-key facts")
    assert facts[0].id != facts[1].id
    selected = next(m for m in facts if "window" in (m.content.parts[0].text or ""))
    await service.delete_memory(app_name="travel", user_id="alice", memory_id=selected.id)
    removed = await agent.call(
        "memory.list", agent_id="writer-b", user_id="alice", session_id="shared", prefix="seat"
    )
    assert removed["entries"] == []
    kept = await agent.memory.get("seat", agent_id="writer-a", user_id="alice", session_id="shared")
    assert kept is not None and kept["value"] == "Prefers aisle seats"
    # A stale delete cannot fall back to the surviving author's row.
    await service.delete_memory(app_name="travel", user_id="alice", memory_id=selected.id)
    kept_again = await agent.memory.get(
        "seat", agent_id="writer-a", user_id="alice", session_id="shared"
    )
    assert kept_again is not None and kept_again["value"] == "Prefers aisle seats"
