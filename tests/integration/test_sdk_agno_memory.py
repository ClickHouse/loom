"""Agno's native memory manager persists user memories through Loom's SDK."""

import base64
import json
from dataclasses import asdict

import pytest

pytest.importorskip("agno")
from agno.db.schemas.memory import UserMemory
from agno.memory.manager import MemoryManager
from loom import SyncAgent
from loom.integrations.agno import LoomMemoryDb

from tests.integration._resources import unique_org


def test_agno_manager_persists_resumes_and_clears_its_user(control_plane_url, mint_test_token):
    org = unique_org("agno")
    token = mint_test_token(org=org, namespaces=["memories"], roles=["writer"])
    with SyncAgent(
        org=org,
        namespace="memories",
        agent="agno",
        base_url=control_plane_url,
        api_key=token,
    ) as agent:
        alice = LoomMemoryDb(agent, user_id="alice")
        bob = LoomMemoryDb(agent, user_id="bob")
        manager = MemoryManager(db=alice)
        key = manager.add_user_memory(
            UserMemory(memory="Aisle seats", memory_id="same", topics=["travel"]), user_id="alice"
        )
        bob.upsert_user_memory(UserMemory(memory="Window seats", memory_id="same"))
        resumed = MemoryManager(db=LoomMemoryDb(agent, user_id="alice"))
        [memory] = resumed.get_user_memories(user_id="alice")
        assert memory.memory_id == key and memory.memory == "Aisle seats"
        assert memory.topics == ["travel"] and memory.user_id == "alice"
        manager.add_user_memory(
            UserMemory(memory="Updated aisle seats", memory_id=key), user_id="alice"
        )
        assert alice.get_user_memory(key).memory == "Updated aisle seats"
        row = agent.call(
            "memory.get",
            {"memory_key": alice._key(key)},
            user_id="alice",
            session_id=alice._session,
        )
        assert row["found"] and row["user_id"] == "alice" and row["memory_type"] == "episodic"
        assert row["agent"] == "agno" and row["source_class"] == "asserted"
        assert row["session_id"] == alice._session
        claims = token.split(".")[1]
        subject = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))["sub"]
        assert row["actor"].endswith(f":{subject}")
        # Native epoch timestamps match Loom's contact-number detector.
        assert row["tags"] == ["sensitivity:medium"]
        complete = UserMemory(
            memory="Tea with the meal",
            memory_id="details",
            user_id="alice",
            topics=["food", "travel"],
            input="What would you like with the meal?",
            feedback="Keep this preference",
            agent_id="planner",
            team_id="travel",
            created_at=10,
            updated_at=20,
        )
        alice.upsert_memories([complete], preserve_updated_at=True)
        restored = LoomMemoryDb(agent, user_id="alice").get_user_memory("details")
        assert asdict(restored) == asdict(complete)
        manager.clear_user_memories(user_id="alice")
        assert alice.get_user_memories() == []
        assert bob.get_user_memory("same").memory == "Window seats"
