"""Working-memory client.

Working memory is a small agent-editable scratchpad loaded by
``context.pack``. It is not part of memory.search and does not create
retrieval embeddings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


DEFAULT_BLOCK_KEY = "scratchpad"
DEFAULT_CHAR_LIMIT = 4096


class WorkingMemoryClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _payload(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        return self._agent.memory._identity(
            user_id,
            session_id,
            run_id=run_id,
            agent_id=agent_id,
        )

    async def get(
        self,
        block_key: str = DEFAULT_BLOCK_KEY,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any] | None:
        payload = self._payload(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        payload["block_key"] = block_key
        result = await self._agent.transport.post("/v1/working_memory.get", payload)
        if not result.get("found"):
            return None
        return result

    async def set(
        self,
        content: str,
        *,
        block_key: str = DEFAULT_BLOCK_KEY,
        char_limit: int = DEFAULT_CHAR_LIMIT,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        payload.update(
            {
                "block_key": block_key,
                "content": content,
                "char_limit": char_limit,
            }
        )
        return await self._agent.transport.post("/v1/working_memory.set", payload)

    async def append(
        self,
        addition: str,
        *,
        block_key: str = DEFAULT_BLOCK_KEY,
        separator: str = "\n",
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        payload.update(
            {
                "block_key": block_key,
                "addition": addition,
                "separator": separator,
            }
        )
        return await self._agent.transport.post("/v1/working_memory.append", payload)

    async def replace(
        self,
        old: str,
        new: str,
        *,
        block_key: str = DEFAULT_BLOCK_KEY,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        payload.update(
            {
                "block_key": block_key,
                "old": old,
                "new": new,
            }
        )
        return await self._agent.transport.post("/v1/working_memory.replace", payload)

    async def delete(
        self,
        block_key: str = DEFAULT_BLOCK_KEY,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._payload(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        payload["block_key"] = block_key
        return await self._agent.transport.post("/v1/working_memory.delete", payload)

    async def render(
        self,
        *,
        block_keys: list[str] | None = None,
        format: str = "xml",
        limit: int = 20,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        payload = self._payload(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        payload.update({"format": format, "limit": limit})
        if block_keys is not None:
            payload["block_keys"] = list(block_keys)
        result = await self._agent.transport.post("/v1/working_memory.render", payload)
        return str(result.get("rendered") or "")

    # ── Trace-and-resume: set/get of a block keyed by run_id ────────────

    async def checkpoint(
        self,
        state: str,
        *,
        run_id: str,
        block_key: str = "checkpoint",
        char_limit: int = DEFAULT_CHAR_LIMIT,
        user_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a run's progress snapshot (overwrites the prior checkpoint).

        Call after each unit of work; ``state`` is whatever the resuming
        process needs to continue (e.g. the list of completed item ids, a
        cursor, or a JSON blob). Survives restarts because the block is
        durable and run-scoped. Read it back with :meth:`resume`.
        """
        return await self.set(
            state,
            block_key=block_key,
            char_limit=char_limit,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )

    async def resume(
        self,
        *,
        run_id: str,
        block_key: str = "checkpoint",
        user_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> str | None:
        """Return a run's last checkpoint ``state``, or ``None`` if the run has
        never checkpointed (a fresh run — start from scratch)."""
        row = await self.get(
            block_key=block_key,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
        )
        return None if row is None else str(row.get("content") or "")
