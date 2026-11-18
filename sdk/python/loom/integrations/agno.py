"""Agno's user-memory database over Loom's synchronous SDK; no additional store."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from typing import Any
from uuid import uuid4

try:
    from agno.db.base import BaseDb
    from agno.db.schemas.memory import UserMemory
    from agno.db.utils import validate_pagination
except ImportError:
    raise ImportError(
        "loom.integrations.agno needs its optional extra: pip install 'clickhouse-loom[agno]'"
    ) from None

from loom import Agent, SyncAgent
from loom.errors import LoomError


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _unsupported(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError(
        "LoomMemoryDb stores Agno user memories only; configure other Agno stores separately"
    )


class LoomMemoryDb(BaseDb):
    """Agno 3.2 memory methods for one bound user and store.

    Give a MemoryManager this database, rather than making it the Agent's session
    database. UserMemory values are episodic Loom rows so native identifiers and
    metadata survive without fact extraction or semantic duplicate merging.
    """

    def __init__(self, agent: SyncAgent, *, user_id: str, store: str = "default") -> None:
        if isinstance(agent, Agent):
            raise TypeError("Agno's synchronous database needs a loom.SyncAgent")
        if user_id in ("", "-", "*") or user_id != user_id.strip():
            raise ValueError("user_id must name an end user")
        if not store.strip():
            raise ValueError("store must not be empty")
        super().__init__(memory_table=store)
        self._store = store
        self._agent = agent
        self._user_id = user_id
        scope = _digest(json.dumps([store, user_id], ensure_ascii=False, separators=(",", ":")))
        self._session = "agno-" + scope
        self._prefix = "agno/" + scope + "/"

    def _user(self, user_id: str | None) -> None:
        if user_id is not None and user_id != self._user_id:
            raise ValueError("Use a separate LoomMemoryDb for each user")

    def _key(self, memory_id: str) -> str:
        if not memory_id:
            raise ValueError("memory_id must not be empty")
        return self._prefix + _digest(memory_id)

    def _call(self, op: str, **body: Any) -> dict[str, Any]:
        return self._agent.call(op, body, user_id=self._user_id, session_id=self._session)

    def _decode(self, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict) or value.get("user_id") != self._user_id:
            raise LoomError("Invalid Agno memory value in this store")
        return value

    def _rows(self) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            page = self._call("memory.list", prefix=self._prefix, limit=100, offset=offset)
            entries = page.get("entries", [])
            for entry in entries:
                key = entry["memory_key"]
                value = entry.get("value")
                if entry.get("value_truncated"):
                    full = self._call("memory.get", memory_key=key)
                    if not full.get("found"):
                        continue
                    value = full["value"]
                rows[key] = self._decode(value)
            if not page.get("truncated"):
                return list(rows.values())
            # Projected pages may be empty, or capped below the requested size.
            offset += max(1, len(entries))

    def table_exists(self, table_name: str) -> bool:
        return table_name == self._store

    def _upsert(
        self, memory: UserMemory, deserialize: bool | None, preserve_updated_at: bool
    ) -> UserMemory | dict[str, Any]:
        self._user(memory.user_id)
        memory.memory_id = memory.memory_id or str(uuid4())
        old_row = self.get_user_memory(memory.memory_id, deserialize=False)
        old = old_row if isinstance(old_row, dict) else {}
        data = asdict(memory)
        data["user_id"] = self._user_id
        now = int(time.time())
        data["created_at"] = (
            old["created_at"]
            if old
            else (data["created_at"] if data["created_at"] is not None else now)
        )
        data["updated_at"] = (
            data["updated_at"] if preserve_updated_at and data["updated_at"] is not None else now
        )
        self._call(
            "memory.set",
            memory_key=self._key(memory.memory_id),
            value=json.dumps(data),
            memory_type="episodic",
        )
        return UserMemory.from_dict(data) if deserialize else data

    def upsert_user_memory(
        self, memory: UserMemory, deserialize: bool | None = True
    ) -> UserMemory | dict[str, Any]:
        return self._upsert(memory, deserialize, False)

    def upsert_memories(
        self,
        memories: list[UserMemory],
        deserialize: bool | None = True,
        preserve_updated_at: bool = False,
    ) -> list[UserMemory | dict[str, Any]]:
        return [self._upsert(memory, deserialize, preserve_updated_at) for memory in memories]

    def get_user_memory(
        self, memory_id: str, deserialize: bool | None = True, user_id: str | None = None
    ) -> UserMemory | dict[str, Any] | None:
        self._user(user_id)
        row = self._call("memory.get", memory_key=self._key(memory_id))
        if not row.get("found"):
            return None
        data = self._decode(row["value"])
        return UserMemory.from_dict(data) if deserialize else data

    def get_user_memories(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        topics: list[str] | None = None,
        search_content: str | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
    ) -> list[UserMemory] | tuple[list[dict[str, Any]], int]:
        self._user(user_id)
        validate_pagination(limit, page)
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        rows = [
            r
            for r in self._rows()
            if (agent_id is None or r.get("agent_id") == agent_id)
            and (team_id is None or r.get("team_id") == team_id)
            and (topics is None or any(t in (r.get("topics") or []) for t in topics))
            and (search_content is None or search_content.lower() in r["memory"].lower())
        ]
        field = sort_by or "updated_at"
        if field not in ("created_at", "updated_at", "memory_id", "user_id", "memory"):
            raise ValueError("Unsupported memory sort field")
        if sort_order is not None and sort_order.lower() not in ("asc", "desc"):
            raise ValueError("sort_order must be asc or desc")
        rows.sort(
            key=lambda r: r.get(field) or (0 if field.endswith("_at") else ""),
            reverse=(sort_order or "desc").lower() == "desc",
        )
        total = len(rows)
        if limit is not None:
            start = ((page or 1) - 1) * limit
            rows = rows[start : start + limit]
        return [UserMemory.from_dict(row) for row in rows] if deserialize else (rows, total)

    def delete_user_memory(self, memory_id: str, user_id: str | None = None) -> None:
        self._user(user_id)
        self._call("memory.delete", memory_key=self._key(memory_id))

    def delete_user_memories(self, memory_ids: list[str], user_id: str | None = None) -> None:
        self._user(user_id)
        for memory_id in memory_ids:
            self.delete_user_memory(memory_id)

    def clear_memories(self) -> None:
        for row in self._rows():
            self.delete_user_memory(row["memory_id"])

    def get_all_memory_topics(self, user_id: str | None = None) -> list[str]:
        self._user(user_id)
        return sorted({topic for row in self._rows() for topic in row.get("topics") or []})

    def get_user_memory_stats(
        self, limit: int | None = None, page: int | None = None, user_id: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        self._user(user_id)
        validate_pagination(limit, page)
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        rows = self._rows()
        if not rows:
            return [], 0
        stats = [
            {
                "user_id": self._user_id,
                "total_memories": len(rows),
                "last_memory_updated_at": max(r.get("updated_at") or 0 for r in rows),
            }
        ]
        start = ((page or 1) - 1) * (limit or 1)
        return stats[start : start + limit] if limit is not None else stats, 1

    # Non-memory BaseDb capabilities fail explicitly rather than using another store.
    calculate_metrics = _unsupported
    create_eval_run = _unsupported
    create_span = _unsupported
    create_spans = _unsupported
    delete_eval_runs = _unsupported
    delete_knowledge_content = _unsupported
    delete_learning = _unsupported
    delete_run = _unsupported
    delete_runs = _unsupported
    delete_session = _unsupported
    delete_sessions = _unsupported
    get_eval_run = _unsupported
    get_eval_runs = _unsupported
    get_knowledge_content = _unsupported
    get_knowledge_contents = _unsupported
    get_latest_schema_version = _unsupported
    get_learning = _unsupported
    get_learnings = _unsupported
    get_metrics = _unsupported
    get_run = _unsupported
    get_runs = _unsupported
    get_session = _unsupported
    get_sessions = _unsupported
    get_span = _unsupported
    get_spans = _unsupported
    get_trace = _unsupported
    get_trace_stats = _unsupported
    get_traces = _unsupported
    rename_eval_run = _unsupported
    rename_session = _unsupported
    upsert_knowledge_content = _unsupported
    upsert_learning = _unsupported
    upsert_schema_version = _unsupported
    upsert_session = _unsupported
    upsert_sessions = _unsupported
    upsert_trace = _unsupported
