"""Decision client — decisions as first-class records.

d = await agent.decision.record(
    category="deploy", scenario="Roll back or hotfix the 26.5 image?",
    reasoning="Rollback is 2 minutes; the fix is untested.", outcome="rollback",
    alternatives=["hotfix forward", "wait for the nightly"], confidence=0.8,
    caused_by=["ci:flaky-runner"], run_id=run["run_id"],
)
prior = await agent.decision.precedents("rollback vs hotfix on a bad image", category="deploy")
chain = await agent.decision.chain(d["memory_key"])
await agent.decision.outcome(d["memory_key"], "rollback took 90s; no user impact", success=True)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from loom.memory import SourceClass
from loom.run import current_run_id

if TYPE_CHECKING:
    from loom.agent import Agent


class DecisionClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def record(
        self,
        *,
        category: str,
        scenario: str,
        reasoning: str,
        outcome: str,
        alternatives: Sequence[str] | None = None,
        confidence: float = 0.0,
        stage: Literal["decided", "proposed"] = "decided",
        decision_maker: str = "",
        caused_by: Sequence[str] | None = None,
        influenced_by: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        valid_at: datetime | None = None,
        files: Sequence[str] | None = None,
        source_resource_id: str = "",
        source_class: SourceClass | None = None,
    ) -> dict[str, Any]:
        """Returns {memory_key, record_id, superseded_prior, edges_written, next}.

        `stage`: "proposed" for an option put forward and not yet chosen or built;
        its statement reads "Proposed ..." instead of "Decided ...".
        `files`: the repo files this decision governs; `memory.for_file` serves it
        before an edit of them. `source_resource_id`: the document, file or URL the
        decision was taken from. `source_class`: "derived", "external" or
        "imported" to declare where it came from; a request may only downgrade.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "agent": self._agent.agent,
            "category": category,
            "scenario": scenario,
            "reasoning": reasoning,
            "outcome": outcome,
            "alternatives": list(alternatives or []),
            "confidence": confidence,
            "decision_maker": decision_maker,
            "caused_by": list(caused_by or []),
            "influenced_by": list(influenced_by or []),
            "tags": list(tags or []),
        }
        if stage != "decided":
            payload["stage"] = stage
        if run_id or current_run_id():
            payload["run_id"] = run_id or current_run_id()
        if session_id is not None:
            payload["session_id"] = session_id
        if user_id is not None:
            payload["user_id"] = user_id
        if valid_at is not None:
            payload["valid_at"] = valid_at.isoformat()
        if isinstance(files, str):
            raise TypeError("files takes a list of paths, not one string")
        if files is not None:
            payload["files"] = list(files)
        if source_resource_id:
            payload["source_resource_id"] = source_resource_id
        if source_class is not None:
            payload["source_class"] = source_class
        return await self._agent.transport.post("/v1/decision.record", payload)

    async def precedents(
        self,
        query: str,
        *,
        category: str = "",
        top_k: int = 5,
        with_outcome_only: bool = False,
    ) -> dict[str, Any]:
        """Returns {precedents: [DecisionRow...], embedding_model}."""
        return await self._agent.transport.post(
            "/v1/decision.precedents",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": "*",
                "query": query,
                "category": category,
                "top_k": top_k,
                "with_outcome_only": with_outcome_only,
            },
        )

    async def chain(
        self,
        memory_key: str,
        *,
        direction: Literal["upstream", "downstream", "both"] = "both",
        max_hops: int = 3,
    ) -> dict[str, Any]:
        return await self._agent.transport.post(
            "/v1/decision.chain",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "memory_key": memory_key,
                "direction": direction,
                "max_hops": max_hops,
            },
        )

    async def outcome(
        self,
        memory_key: str,
        realized_outcome: str,
        *,
        success: bool | None = None,
    ) -> dict[str, Any]:
        """Returns {memory_key, updated, memories_credited}."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "memory_key": memory_key,
            "realized_outcome": realized_outcome,
        }
        if success is not None:
            payload["success"] = success
        return await self._agent.transport.post("/v1/decision.outcome", payload)
