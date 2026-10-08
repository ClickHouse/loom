"""History-policy client — rules over the agent's own trace.

Roles say who may call what; a policy rule says what the caller's history
must look like, evaluated over loom.agent_events at call time:

    # ADMIN: no more than 200 writes an hour, per agent, durably
    await agent.policy.set(
        "write-cap", effect="forbid", actions=["memory.set", "memory.set_from_messages"],
        condition={"op": "count_within", "within": "1h",
                   "operation": ["memory.set", "memory.set_from_messages"],
                   "cmp": "gte", "value": 200},
    )
    # anyone: would this call be allowed right now, and why?
    verdict = await agent.policy.check("memory.delete", args={"memory_key": "prefs"})
    if verdict["decision"] == "deny":
        ...  # verdict["determining_rules"], verdict["rules"][i]["leaves"]

Server mode (`LOOM_POLICY_MODE`) decides whether a denial blocks: `off`
evaluates nothing, `shadow` records what enforce would have done in the
decisions ledger (`agent.policy.decisions()`), `enforce` returns
403 POLICY_FORBIDDEN with the same structured verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from loom.agent import Agent

Effect = Literal["forbid", "require"]


class PolicyClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def set(
        self,
        policy_id: str,
        *,
        effect: Effect,
        actions: Sequence[str],
        condition: dict[str, Any],
        description: str = "",
        enabled: bool = True,
        mode: Literal["inherit", "shadow", "enforce"] = "inherit",
        created_by: str = "",
    ) -> dict[str, Any]:
        """Create or replace a rule. ADMIN role required. Returns
        {rule, leaf_count, widening, change}; a malformed condition is a 422.
        `mode` overrides the deployment's LOOM_POLICY_MODE for this rule
        (shadow = record only, enforce = block) except when it is off."""
        return await self._agent.transport.post(
            "/v1/policy.set",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "policy_id": policy_id,
                "effect": effect,
                "actions": list(actions),
                "condition": condition,
                "description": description,
                "enabled": enabled,
                "mode": mode,
                "created_by": created_by,
            },
        )

    async def list(self, *, include_disabled: bool = True) -> dict[str, Any]:
        """Rules in this namespace plus the server's `mode`."""
        return await self._agent.transport.post(
            "/v1/policy.list",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "include_disabled": include_disabled,
            },
        )

    async def delete(self, policy_id: str, *, removed_by: str = "") -> dict[str, Any]:
        """Tombstone a rule. ADMIN role required. Returns {removed}."""
        return await self._agent.transport.post(
            "/v1/policy.delete",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "policy_id": policy_id,
                "removed_by": removed_by,
            },
        )

    async def check(
        self,
        operation: str,
        *,
        args: dict[str, Any] | None = None,
        agent: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        at: str | None = None,
        rules: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Dry run: what would the rules decide for this call right now?
        Never blocks. Defaults to this Agent's identity; pass `rules` to
        evaluate candidate rules instead of the stored ones."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "operation": operation,
            "agent": agent if agent is not None else self._agent.agent,
            "args": dict(args or {}),
        }
        if user_id is not None:
            payload["user_id"] = user_id
        if session_id is not None:
            payload["session_id"] = session_id
        if at is not None:
            payload["at"] = at
        if rules is not None:
            payload["rules"] = [dict(r) for r in rules]
        return await self._agent.transport.post("/v1/policy.check", payload)

    async def decisions(
        self,
        *,
        decision: Literal["allow", "deny"] | None = None,
        limit: int = 100,
    ) -> Sequence[dict[str, Any]]:
        """The gate's ledger for this namespace, newest first."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "limit": limit,
        }
        if decision is not None:
            payload["decision"] = decision
        out = await self._agent.transport.post("/v1/policy.decisions", payload)
        return list(out.get("rows", []))
