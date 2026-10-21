"""Learnings client — human-in-the-loop review queue for memory promotion.

This client lets a reviewer agent list pending candidates and
promote / reject them.

Surface:
  - list(status='pending') → candidate rows
  - review(candidate_id, decision) → flips status; on 'promote' writes
    it to its destination (memory or knowledge)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from loom.agent import Agent

Status = Literal["pending", "promoted", "rejected"]
Decision = Literal["promote", "reject"]


class LearningsClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def list(
        self,
        *,
        status: Status = "pending",
        limit: int = 50,
        source_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """List candidates in the namespace's review queue.

        `source_kind` filter: restrict to one seeder, e.g.
        'memory_supporters', 'conversation_seeder' or 'assistant_turn'.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "status": status,
            "limit": limit,
        }
        if source_kind is not None:
            payload["source_kind"] = source_kind
        result = await self._agent.transport.post(
            "/v1/learnings.list",
            payload,
        )
        rows: list[dict[str, Any]] = result.get("rows", [])
        return rows

    async def review(
        self,
        candidate_id: str,
        decision: Decision,
        *,
        destination: str | None = None,
        reason: str = "",
        approver: str = "learnings-reviewer",
    ) -> dict[str, Any]:
        """Promote or reject one candidate.

        On 'promote', `destination` chooses where it lands:
          - 'memory'    → loom.shared for a supporters candidate, a namespace
                          memory row for a conversation-seeded fact, the
                          agent's user_id '-' row for a bot's rule (assistant_turn)
          - 'knowledge' → a document, knowledge.upsert(source_uri='learning://<cid>')
          - 'both'      → both, memory first
          - None        → 'knowledge' for a bot's rule (assistant_turn), else 'memory'

        On 'reject' the queue row's status flips and nothing else runs.
        Idempotent: re-reviewing a decided candidate returns its state, except
        a bot's rule whose last supporter was erased, which answers 404.
        """
        return await self._agent.transport.post(
            "/v1/learnings.review",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "candidate_id": candidate_id,
                "decision": decision,
                **({"destination": destination} if destination is not None else {}),
                "reason": reason,
                "approver": approver,
            },
        )
