"""Conflicts client — semantic conflict surface.

When memory.set produces a semantic conflict (two close-but-different
memories in the same scope), both rows are flagged status='conflicted'.
This client exposes that triage surface to agents + scripts.

  agent.conflicts.list(...)         → enumerate active conflicts
  agent.conflicts.resolve(memory_id, decision, ...) → close a conflict

Decisions:
  - "revise":   memory_id wins; peer(s) tombstoned
  - "contract": memory_id is wrong; tombstoned; peer(s) restored to active
  - "allow":    both legitimately different; flip all back to active
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from loom.agent import Agent

ResolveDecision = Literal["revise", "contract", "allow"]


class ConflictsClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def list(
        self,
        *,
        limit: int = 50,
        before_version: int | None = None,
    ) -> dict[str, Any]:
        """Enumerate active conflicts in the agent's namespace.

        Returns `{"rows": [...], "next_before_version": int | None}`.
        Pass the previous response's `next_before_version` to paginate.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "limit": limit,
        }
        if before_version is not None:
            payload["before_version"] = before_version
        return await self._agent.transport.post("/v1/conflicts.list", payload)

    async def resolve(
        self,
        memory_id: str,
        decision: ResolveDecision,
        *,
        peer_id: str | None = None,
        reason: str = "",
        resolved_by: str = "",
    ) -> dict[str, Any]:
        """Close a conflict pair. See module docstring for decision semantics."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "memory_id": memory_id,
            "decision": decision,
            "reason": reason,
            "resolved_by": resolved_by,
        }
        if peer_id is not None:
            payload["peer_id"] = peer_id
        return await self._agent.transport.post("/v1/conflicts.resolve", payload)

    async def allowlist_list(self, *, limit: int = 100) -> builtins.list[dict[str, Any]]:
        """List allow-pairs in this namespace — (key_a, key_b) tuples
        the operator has previously decided are legitimately distinct.
        Each one bypasses future conflict-flag on writes between those
        two keys."""
        r = await self._agent.transport.post(
            "/v1/conflicts.allowlist_list",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "limit": limit,
            },
        )
        return list(r.get("rows", []))

    async def allowlist_remove(
        self,
        memory_key_a: str,
        memory_key_b: str,
        *,
        reason: str = "",
        removed_by: str = "",
    ) -> dict[str, Any]:
        """Revoke an allow decision. Future close-but-different writes
        between the two keys re-flag as conflicts. Caller can pass the
        keys in any order — the server canonicalizes (min, max)."""
        return await self._agent.transport.post(
            "/v1/conflicts.allowlist_remove",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "memory_key_a": memory_key_a,
                "memory_key_b": memory_key_b,
                "reason": reason,
                "removed_by": removed_by,
            },
        )
