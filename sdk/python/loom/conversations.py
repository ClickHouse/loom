"""Conversations client.

Append and query message-level chat history that persists across
sessions. Your chat surface or agent code calls this to record threads;
Loom itself is not the live chat surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from loom.memory import SENTINEL

if TYPE_CHECKING:
    from loom.agent import Agent

ChatRole = Literal["user", "assistant", "system", "tool"]


class ConversationsClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _identity(self, agent_id: str | None = None) -> dict[str, str]:
        return {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "agent": agent_id or self._agent.agent,
        }

    async def create(
        self,
        thread_id: str,
        *,
        title: str = "",
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Open a thread container with title + metadata + run_id.

        Optional — `append()` works without `create()` (the thread comes
        into existence on first append). Use this for a stable named
        thread: a title, `run_id` binding, and thread-level metadata
        rather than per-message.
        """
        payload: dict[str, Any] = self._identity(agent_id)
        payload.update(
            {
                "thread_id": thread_id,
                "title": title,
                "metadata": metadata or {},
                "run_id": run_id or SENTINEL,
                "user_id": user_id or SENTINEL,
            }
        )
        return await self._agent.transport.post(
            "/v1/conversation.create",
            payload,
            idempotency_key=thread_id,
        )

    async def append(
        self,
        thread_id: str,
        msg_id: str,
        role: ChatRole,
        content: str,
        *,
        reasoning: str = "",
        user_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tokens: int = 0,
    ) -> dict[str, Any]:
        """Append one message to a thread.

        `msg_id` is caller-provided (ULID recommended) so the server can
        dedup retries on transport errors. Use `reasoning` to attach the
        model's chain-of-thought as a first-class column distinct from
        `content`.
        """
        payload: dict[str, Any] = self._identity(agent_id)
        payload.update(
            {
                "thread_id": thread_id,
                "msg_id": msg_id,
                "role": role,
                "content": content,
                "reasoning": reasoning,
                "user_id": user_id or SENTINEL,
                "run_id": run_id or SENTINEL,
                "metadata": metadata or {},
                "tokens": tokens,
            }
        )
        return await self._agent.transport.post(
            "/v1/conversation.append",
            payload,
            idempotency_key=msg_id,
        )

    async def get(
        self,
        thread_id: str,
        *,
        include_messages: bool = False,
        limit: int = 100,
        offset: int = 0,
        order: Literal["asc", "desc"] = "asc",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one thread by id: metadata + optional messages.

        `include_messages=False` (default) returns the thread row plus a
        message COUNT only; set it `True` to also fetch a message slice
        for replay. `order="asc"` (default) pages oldest-first;
        `order="desc"` pages from the tail — `limit=N, offset=0` is the
        newest N turns, newest first.

        Returns `{found, thread_id, title, metadata, run_id, user_id,
        agent, created_at, updated_at, msg_count, messages: [...]}`.
        """
        payload: dict[str, Any] = self._identity(agent_id)
        payload.update(
            {
                "thread_id": thread_id,
                "include_messages": include_messages,
                "limit": limit,
                "offset": offset,
                "order": order,
            }
        )
        return await self._agent.transport.post("/v1/conversation.get", payload)

    async def update(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Rename / relabel one thread.

        Updates `title` and/or `metadata` and/or `run_id` on an existing
        thread. At least one must be passed (no-op calls 400). Returns
        `{updated: bool, thread_id, updated_at}` — `updated=False`
        means the thread doesn't exist.
        """
        payload: dict[str, Any] = self._identity(agent_id)
        payload["thread_id"] = thread_id
        if title is not None:
            payload["title"] = title
        if metadata is not None:
            payload["metadata"] = metadata
        if run_id is not None:
            payload["run_id"] = run_id
        return await self._agent.transport.post("/v1/conversation.update", payload)

    async def delete(
        self,
        thread_id: str,
        *,
        confirm_thread_id: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Soft-delete one thread (ADMIN role).

        Tombstones the thread row, its messages, and the embeddings no other
        agent of the thread still holds with the same text (its first 240
        characters); retention TTL
        eventually hard-deletes, and read paths filter tombstoned rows out.

        `confirm_thread_id` must equal `thread_id`, guarding against
        accidental deletes. This is the bounded "remove this one chat"
        primitive; for user- or namespace-wide erasure use the
        `erasure.*` family.
        """
        payload: dict[str, Any] = self._identity(agent_id)
        payload["thread_id"] = thread_id
        payload["confirm_thread_id"] = confirm_thread_id
        return await self._agent.transport.post("/v1/conversation.delete", payload)

    async def list(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Paginated chronological listing of a thread's history."""
        payload: dict[str, Any] = self._identity(agent_id)
        payload.update(
            {
                "thread_id": thread_id,
                "limit": limit,
            }
        )
        if run_id is not None:
            payload["run_id"] = run_id
        if cursor is not None:
            payload["cursor"] = cursor
        return await self._agent.transport.post("/v1/conversation.list", payload)

    async def search(
        self,
        query: str,
        *,
        thread_id: str | None = None,
        role: ChatRole | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        top_k: int = 20,
    ) -> dict[str, Any]:
        """Semantic search across the namespace's conversations.

        The query is embedded and hits are ranked by cosine similarity
        over the stored turn embeddings (server-side minimum similarity,
        recency breaks ties); the server falls back to a case-insensitive
        substring match only when the embedder is unavailable. Scope to
        one thread by passing `thread_id`; filter by speaker by passing
        `role` or by work item by passing `run_id`.
        """
        payload: dict[str, Any] = self._identity(agent_id)
        payload.update(
            {
                "query": query,
                "top_k": top_k,
            }
        )
        if thread_id is not None:
            payload["thread_id"] = thread_id
        if role is not None:
            payload["role"] = role
        if run_id is not None:
            payload["run_id"] = run_id
        return await self._agent.transport.post("/v1/conversation.search", payload)
