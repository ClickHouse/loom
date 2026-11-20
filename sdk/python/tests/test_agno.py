"""Agno MemoryManager reads, revises and clears its bound user's Loom memories."""

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from agno.db.schemas.memory import UserMemory
from agno.memory.manager import MemoryManager
from loom import SyncAgent
from loom.integrations.agno import LoomMemoryDb
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
)


@dataclass
class _TransportState:
    session_protection: SessionProtection | None = None


class FakeAgent:
    def __init__(self, cap=100, unserved=0):
        self.rows = {}
        self.calls = []
        self.cap = cap
        self.unserved = unserved
        self.transport = _TransportState()

    def call(self, op, body, **scope):
        self.calls.append((op, body, scope))
        key = (scope["user_id"], scope["session_id"], body.get("memory_key"))
        if op == "memory.set":
            self.rows[key] = json.loads(body["value"])
            return {"stored": True}
        if op == "memory.get":
            return {"found": key in self.rows, "value": self.rows.get(key)}
        if op == "memory.delete":
            self.rows.pop(key, None)
            return {}
        if op == "memory.list":
            rows = [
                (k, v)
                for k, v in self.rows.items()
                if k[:2] == key[:2] and k[2].startswith(body["prefix"])
            ]
            projected = [None] * self.unserved + rows
            count = min(body["limit"], self.cap)
            entries = []
            for row in projected[body["offset"] : body["offset"] + count]:
                if row is not None:
                    k, v = row
                    cut = len(json.dumps(v)) > 2048
                    entries.append(
                        {"memory_key": k[2], "value": None if cut else v, "value_truncated": cut}
                    )
            return {"entries": entries, "truncated": body["offset"] + count < len(projected)}
        raise AssertionError(op)


def test_native_manager_roundtrip_update_metadata_and_clear():
    agent = FakeAgent()
    db = LoomMemoryDb(agent, user_id="alice")
    manager = MemoryManager(db=db)
    key = manager.add_user_memory(
        UserMemory(memory="aisle", topics=["travel"], agent_id="planner"), user_id="alice"
    )
    assert key
    [memory] = manager.get_user_memories(user_id="alice")
    assert memory.memory_id == key and memory.memory == "aisle"
    assert memory.agent_id == "planner" and memory.topics == ["travel"]
    manager.add_user_memory(UserMemory(memory="window", memory_id=key), user_id="alice")
    [memory] = manager.get_user_memories(user_id="alice")
    assert memory.memory == "window"
    manager.clear_user_memories(user_id="alice")
    assert manager.get_user_memories(user_id="alice") == []
    assert all(scope["user_id"] == "alice" for _, _, scope in agent.calls)
    assert all(
        body.get("memory_type") == "episodic" for op, body, _ in agent.calls if op == "memory.set"
    )


def test_same_native_id_is_separate_for_users_and_stores():
    agent = FakeAgent()
    mine = LoomMemoryDb(agent, user_id="alice")
    bob = LoomMemoryDb(agent, user_id="bob")
    other = LoomMemoryDb(agent, user_id="alice", store="other")
    for db in (mine, bob, other):
        db.upsert_user_memory(UserMemory(memory_id="same", memory="same"))
    assert len(agent.rows) == 3
    mine.clear_memories()
    assert mine.get_user_memory("same") is None
    assert bob.get_user_memory("same").memory == "same"
    assert other.get_user_memory("same").memory == "same"
    with pytest.raises(ValueError, match="each user"):
        mine.get_user_memories(user_id="bob")
    with pytest.raises(ValueError, match="each user"):
        mine.upsert_user_memory(UserMemory(memory="no", user_id="bob"))


def test_paging_survives_empty_projected_pages_reader_caps_and_long_values():
    agent = FakeAgent(cap=2, unserved=4)
    db = LoomMemoryDb(agent, user_id="alice")
    for i in range(6):
        db.upsert_user_memory(UserMemory(memory_id=str(i), memory=str(i) + "x" * 5000))
    got = db.get_user_memories(sort_by="memory_id", sort_order="asc")
    assert [m.memory_id for m in got] == [str(i) for i in range(6)]
    assert len(got[0].memory) == 5001
    assert any(op == "memory.get" for op, _, _ in agent.calls)


def test_filters_pagination_topics_and_statistics():
    db = LoomMemoryDb(FakeAgent(), user_id="alice")
    db.upsert_memories(
        [
            UserMemory(memory_id="one", memory="Aisle seats", topics=["travel"], agent_id="a"),
            UserMemory(memory_id="two", memory="Tea", topics=["food"], team_id="t"),
        ]
    )
    rows, total = db.get_user_memories(
        search_content="AISLE", topics=["travel"], agent_id="a", deserialize=False
    )
    assert total == 1 and rows[0]["memory_id"] == "one"
    assert db.get_user_memories(team_id="t")[0].memory_id == "two"
    assert db.get_all_memory_topics() == ["food", "travel"]
    assert len(db.get_user_memories(limit=1, page=2)) == 1
    stats, total = db.get_user_memory_stats()
    assert total == 1 and stats[0]["total_memories"] == 2
    db.delete_user_memories(["one", "two"])
    assert db.get_user_memory_stats() == ([], 0)


def test_bulk_preserves_requested_timestamps_and_values_are_detached(monkeypatch):
    monkeypatch.setattr("loom.integrations.agno.time.time", lambda: 100)
    db = LoomMemoryDb(FakeAgent(), user_id="alice")
    original = UserMemory(
        memory="aisle", memory_id="one", topics=["travel"], created_at=10, updated_at=20
    )
    [saved] = db.upsert_memories([original], preserve_updated_at=True)
    assert saved.created_at == 10 and saved.updated_at == 20
    original.topics.append("mutated")
    assert db.get_user_memory("one").topics == ["travel"]
    saved = db.upsert_user_memory(UserMemory(memory="updated", memory_id="one"))
    assert saved.created_at == 10 and saved.updated_at == 100


def test_non_memory_operations_refuse_without_another_store():
    db = LoomMemoryDb(FakeAgent(), user_id="alice")
    with pytest.raises(NotImplementedError, match="user memories only"):
        db.get_session("s")
    assert db.table_exists(db.memory_table_name)
    assert not db.table_exists(db.session_table_name)
    for user in ["", " ", "-", "*", " alice", "alice "]:
        with pytest.raises(ValueError):
            LoomMemoryDb(FakeAgent(), user_id=user)


def test_mixed_preserved_and_normal_epochs_sort_and_report_statistics():
    db = LoomMemoryDb(FakeAgent(), user_id="alice")
    db.upsert_memories(
        [UserMemory(memory="imported", memory_id="old", updated_at=20)], preserve_updated_at=True
    )
    db.upsert_user_memory(UserMemory(memory="normal", memory_id="new"))
    rows, total = db.get_user_memories(deserialize=False)
    assert total == 2 and all(isinstance(r["updated_at"], int) for r in rows)
    assert db.get_user_memory_stats()[0][0]["total_memories"] == 2


@pytest.mark.parametrize("method", ["get_user_memories", "get_user_memory_stats"])
def test_pagination_without_limit_is_refused_before_read(method):
    agent = FakeAgent()
    db = LoomMemoryDb(agent, user_id="alice")
    with pytest.raises(ValueError, match="without.*limit"):
        getattr(db, method)(page=2)
    assert not agent.calls


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("get_run", {"run_id": "run"}),
        ("get_runs", {}),
        ("get_runs", {"deserialize": False}),
        ("delete_run", {"run_id": "run"}),
        ("delete_runs", {"run_ids": ["run"]}),
    ],
)
def test_inherited_run_operations_refuse_without_another_store(method, kwargs):
    agent = FakeAgent()
    db = LoomMemoryDb(agent, user_id="alice")
    with pytest.raises(NotImplementedError, match="user memories only"):
        getattr(db, method)(**kwargs)
    assert not agent.calls


@pytest.mark.parametrize("method", ["get_user_memories", "get_user_memory_stats"])
def test_negative_limit_is_refused_before_read(method):
    agent = FakeAgent()
    db = LoomMemoryDb(agent, user_id="alice")
    with pytest.raises(ValueError, match="limit.*non-negative"):
        getattr(db, method)(limit=-1)
    assert not agent.calls


@contextmanager
def _protection_sync_agent(
    root: SessionProtection | None,
    reply: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> Iterator[tuple[SyncAgent, list[tuple[str, dict[str, Any]]]]]:
    posts: list[tuple[str, dict[str, Any]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        op = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        posts.append((op, body))
        return httpx.Response(200, json=reply(op, body))

    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("NO_PROXY", "*")
        environment.setenv("no_proxy", "*")
        with SyncAgent(
            org="acme",
            namespace="support",
            agent="bot",
            base_url="http://inert.invalid",
            api_key="test-token",
            session_protection=root,
            pacing=False,
            coalesce_sets=False,
            auto_run=False,
            mentor_brief=False,
            retry={"max_attempts": 1},
        ) as agent:
            agent.transport._client._transport = httpx.MockTransport(respond)
            yield agent, posts


def test_protection_sync_agent_mock_transport_ignores_inherited_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("no_proxy", "localhost")

    def reply(op: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("Transport selection must not send a request")

    with _protection_sync_agent(None, reply) as (agent, posts):
        selected = agent.transport._client._transport_for_url(
            httpx.URL("http://inert.invalid/v1/memory.list")
        )
        assert isinstance(selected, httpx.MockTransport)
        assert not posts
    assert os.environ["NO_PROXY"] == "localhost"
    assert os.environ["no_proxy"] == "localhost"


def _protection_reply(op: str, body: dict[str, Any]) -> dict[str, Any]:
    if op == "memory.list":
        return {"entries": [], "truncated": False}
    if op == "memory.get":
        return {"found": False}
    if op == "memory.set":
        return {"stored": True}
    raise AssertionError(op)


def test_protection_db_observes_constructor_family_after_binding_changes(tmp_path: Path) -> None:
    first = SessionProtection.create(base=tmp_path / "first")
    current = SessionProtection.create(base=tmp_path / "current")

    def reply(op: str, body: dict[str, Any]) -> dict[str, Any]:
        assert op == "memory.list"
        return {"entries": [], "mirrored": True}

    with _protection_sync_agent(first, reply) as (agent, posts):
        db = LoomMemoryDb(agent, user_id="alice")
        manager = MemoryManager(db=db)
        agent.transport.session_protection = current
        assert manager.get_user_memories(user_id="alice") == []
        assert first.protected and current.protected
        assert [op for op, _ in posts] == ["memory.list"]


@pytest.mark.parametrize("origin", ["constructor", "callback"])
def test_protection_db_retains_ambient_observers_after_scope_exit(
    tmp_path: Path, origin: str
) -> None:
    earlier = SessionProtection.create(base=tmp_path / "earlier")
    current = SessionProtection.create(base=tmp_path / "current")
    marked = False

    def reply(op: str, body: dict[str, Any]) -> dict[str, Any]:
        assert op == "memory.list"
        return {"entries": [], "mirrored": marked}

    with _protection_sync_agent(None, reply) as (agent, posts):
        if origin == "constructor":
            with observation_scope((earlier,)):
                db = LoomMemoryDb(agent, user_id="alice")
            manager = MemoryManager(db=db)
        else:
            db = LoomMemoryDb(agent, user_id="alice")
            manager = MemoryManager(db=db)
            with observation_scope((earlier,)):
                assert manager.get_user_memories(user_id="alice") == []
        posts.clear()
        marked = True
        agent.transport.session_protection = current
        assert manager.get_user_memories(user_id="alice") == []
        assert earlier.protected and current.protected
        assert [op for op, _ in posts] == ["memory.list"]


@pytest.mark.parametrize("method", ["read", "upsert"])
def test_protection_db_automatic_callback_retains_protected_constructor(
    tmp_path: Path, method: str
) -> None:
    first = SessionProtection.create(base=tmp_path / "first")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")
    with _protection_sync_agent(first, _protection_reply) as (agent, posts):
        db = LoomMemoryDb(agent, user_id="alice")
        manager = MemoryManager(db=db)
        agent.transport.session_protection = current
        first.mark()
        with producer.automatic():
            if method == "read":
                with pytest.raises(AutomaticCaptureSuppressed):
                    manager.get_user_memories(user_id="alice")
            else:
                result = manager._upsert_db_memory(
                    UserMemory(memory_id="one", memory="private", user_id="alice")
                )
                assert result == "Error adding memory"
        assert not posts


def test_protection_db_rechecks_retained_family_after_credentials_await(tmp_path: Path) -> None:
    first = SessionProtection.create(base=tmp_path / "first")
    callback = SessionProtection.create(base=tmp_path / "callback")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    producer = SessionProtection.create(base=tmp_path / "producer")
    with _protection_sync_agent(first, _protection_reply) as (agent, posts):
        db = LoomMemoryDb(agent, user_id="alice")
        agent.transport.session_protection = callback

        async def credentials_ready() -> None:
            first.mark()
            agent.transport.session_protection = replacement

        class Auth(httpx.Auth):
            async def async_auth_flow(self, request: httpx.Request) -> Any:
                await credentials_ready()
                yield request

        agent.transport._client.auth = Auth()
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            db.upsert_memories([UserMemory(memory_id="one", memory="private")])
        assert not posts


def test_protection_db_malformed_response_marks_retained_and_current_families(
    tmp_path: Path,
) -> None:
    first = SessionProtection.create(base=tmp_path / "first")
    current = SessionProtection.create(base=tmp_path / "current")

    def reply(op: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"found": False, "mirrored": "unknown"}

    with _protection_sync_agent(first, reply) as (agent, posts):
        db = LoomMemoryDb(agent, user_id="alice")
        agent.transport.session_protection = current
        with pytest.raises(SessionProtectionUnavailable):
            db.get_user_memory("one")
        assert first.protected and current.protected
        assert [op for op, _ in posts] == ["memory.get"]


def test_protection_db_retained_mark_failure_withholds_output_and_marks_other_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = SessionProtection.create(base=tmp_path / "first")
    current = SessionProtection.create(base=tmp_path / "current")
    mark = SessionProtection.mark

    def fail_first(root: SessionProtection) -> None:
        if root == first:
            raise SessionProtectionUnavailable()
        mark(root)

    def reply(op: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"entries": [], "mirrored": True}

    with _protection_sync_agent(first, reply) as (agent, posts):
        db = LoomMemoryDb(agent, user_id="alice")
        agent.transport.session_protection = current
        monkeypatch.setattr(SessionProtection, "mark", fail_first)
        with pytest.raises(SessionProtectionUnavailable):
            MemoryManager(db=db).get_user_memories(user_id="alice")
        assert current.protected
        assert [op for op, _ in posts] == ["memory.list"]


@pytest.mark.parametrize("method", ["list", "upsert"])
def test_protection_db_first_marked_response_stops_followups_and_marks_retained_family(
    tmp_path: Path, method: str
) -> None:
    first = SessionProtection.create(base=tmp_path / "first")
    current = SessionProtection.create(base=tmp_path / "current")
    producer = SessionProtection.create(base=tmp_path / "producer")

    def reply(op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.list":
            return {
                "mirrored": True,
                "truncated": True,
                "entries": [{"memory_key": "held", "value_truncated": True}],
            }
        if op == "memory.set":
            return {"stored": True, "mirrored": True}
        return _protection_reply(op, body)

    with _protection_sync_agent(first, reply) as (agent, posts):
        db = LoomMemoryDb(agent, user_id="alice")
        agent.transport.session_protection = current
        with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            if method == "list":
                MemoryManager(db=db).get_user_memories(user_id="alice")
            else:
                db.upsert_memories(
                    [
                        UserMemory(memory_id="one", memory="first"),
                        UserMemory(memory_id="two", memory="second"),
                    ]
                )
        assert [op for op, _ in posts] == (
            ["memory.list"] if method == "list" else ["memory.get", "memory.set"]
        )
        assert first.protected and current.protected and producer.protected


@pytest.mark.parametrize("method", ["read", "upsert"])
def test_protection_db_explicit_operation_stays_explicit_after_automatic_callback(
    tmp_path: Path, method: str
) -> None:
    family = SessionProtection.create(base=tmp_path / "family")
    with _protection_sync_agent(family, _protection_reply) as (agent, posts):
        db = LoomMemoryDb(agent, user_id="alice")
        manager = MemoryManager(db=db)
        with family.automatic():
            assert manager.get_user_memories(user_id="alice") == []
        family.mark()
        posts.clear()
        if method == "read":
            assert manager.get_user_memories(user_id="alice") == []
        else:
            row = db.upsert_user_memory(UserMemory(memory_id="one", memory="explicit"))
            assert row.memory_id == "one" and row.memory == "explicit"
        assert [op for op, _ in posts] == (
            ["memory.list"] if method == "read" else ["memory.get", "memory.set"]
        )
