"""Procedure client — procedural memory plane SDK surface.

Wraps the `procedure.add` / `procedure.match` / `procedure.list` /
`procedure.locate` / `procedure.record_run` / `procedure.evolve` /
`procedure.revise` wire ops. Procedural memory is "how to do things" — learned skills,
patterns, workflows — distinct from semantic memory (durable facts
about the world) and episodic (specific past events).

    # Register a procedure
    await agent.procedure.add(
        name="rollback_deploy",
        situation="Production starts emitting errors after a deploy",
        steps=[
            "Revert to the previous git tag",
            "Restart all replicas",
            "Verify health endpoint returns 200",
        ],
        tags=["ops", "incident"],
    )

    # Recall the right procedure given a situation
    matches = await agent.procedure.match(
        situation="Customers are seeing 500s right after we deployed",
        top_k=3,
    )
    for m in matches["matches"]:
        print(m["name"], m["situation"], "→", m["steps"])

The `situation` field drives the embedding, so match() retrieves by
precondition-similarity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class ProcedureClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def add(
        self,
        name: str,
        situation: str,
        steps: list[str],
        *,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a procedure. The `situation` text is the precondition
        (when this procedure applies); it gets embedded so match() can
        retrieve by situation-similarity. The `steps` list is the
        procedure body the consuming agent reads."""
        return await self._agent.transport.post(
            "/v1/procedure.add",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "name": name,
                "situation": situation,
                "steps": list(steps),
                "tags": list(tags or []),
                "metadata": dict(metadata or {}),
            },
        )

    async def match(
        self,
        situation: str,
        *,
        top_k: int = 5,
        tags: list[str] | None = None,
        namespace_scope: str = "all_mine",
        goal: str | None = None,
    ) -> dict[str, Any]:
        """Find procedures whose stored `situation` is semantically
        closest to `situation`. Returns the top-K with the full
        procedure body (name, situation, steps, tags, metadata) so
        the agent can pick + execute.

        `goal` (what the agent is trying to achieve, as distinct from the
        state it is in) makes the match two-factor: a procedure must fit
        both its precondition and its goal; matches then carry
        `goal_similarity`.

        Returns `{"matches": [{name, situation, steps, tags, metadata,
        similarity, memory_key, namespace}]}`.
        """
        return await self._agent.transport.post(
            "/v1/procedure.match",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": "*",  # fan out across all agents
                "situation": situation,
                **({"goal": goal} if goal else {}),
                "top_k": top_k,
                "tags": list(tags or []),
                "namespace_scope": namespace_scope,
            },
        )

    async def locate(
        self,
        memory_key: str,
        *,
        at: str = "",
        recent_path: list[str] | None = None,
        hops: int = 2,
    ) -> dict[str, Any]:
        """Where am I in this procedure and what comes next: the active node
        (matched by node id or tool name) and only the transitions up to
        `hops` ahead, with condition / guidance / pitfalls, outcome counts
        and a prompt-ready `rendered` text."""
        return await self._agent.transport.post(
            "/v1/procedure.locate",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "memory_key": memory_key,
                "at": at,
                "recent_path": list(recent_path or []),
                "hops": hops,
            },
        )

    async def record_run(
        self,
        memory_key: str,
        path: list[str],
        *,
        ok: bool = True,
        note: str = "",
        steps_taken: int | None = None,
        duration_ms: int = 0,
        tokens: int = 0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Report one execution: the ordered node ids visited and whether the
        goal was met. One append to the run ledger. `steps_taken` (defaults
        to the path length), `duration_ms` and `tokens` are what the run
        cost; the acceptance gate prefers the cheaper of two equally
        successful revisions. 0 = not reported."""
        return await self._agent.transport.post(
            "/v1/procedure.record_run",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "session_id": session_id or "-",
                "memory_key": memory_key,
                **({"steps_taken": steps_taken} if steps_taken is not None else {}),
                **({"duration_ms": duration_ms} if duration_ms else {}),
                **({"tokens": tokens} if tokens else {}),
                "path": list(path),
                "ok": bool(ok),
                "note": note,
            },
        )

    async def evolve(
        self,
        memory_key: str,
        *,
        min_runs: int = 3,
        gate_min_runs: int = 5,
        gate_margin: float = 0.05,
    ) -> dict[str, Any]:
        """Consolidate recorded runs: proven path, dead ends, fail-prone
        steps, template promotion, and the accept/revert verdict on a
        pending revision."""
        return await self._agent.transport.post(
            "/v1/procedure.evolve",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "memory_key": memory_key,
                "min_runs": min_runs,
                "gate_min_runs": gate_min_runs,
                "gate_margin": gate_margin,
            },
        )

    async def revise(
        self,
        memory_key: str,
        graph: dict[str, Any],
        *,
        reason: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Propose an edited topology. Keep node ids you did not change so
        their statistics carry over. The next evolve with enough runs on both
        sides accepts it or reverts it."""
        return await self._agent.transport.post(
            "/v1/procedure.revise",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "memory_key": memory_key,
                "graph": graph,
                "reason": reason,
                "force": bool(force),
            },
        )

    async def fork(
        self,
        from_namespace: str,
        memory_key: str,
        *,
        new_name: str = "",
        reason: str = "",
        from_org: str = "",
    ) -> dict[str, Any]:
        """Adopt a procedure from a namespace this agent can read into its
        own, with statistics, flags and revisions reset and `forked_from`
        lineage kept."""
        return await self._agent.transport.post(
            "/v1/procedure.fork",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "from_namespace": from_namespace,
                "from_org": from_org,
                "memory_key": memory_key,
                "new_name": new_name,
                "reason": reason,
            },
        )

    async def list(
        self,
        *,
        prefix: str = "",
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Enumerate registered procedures in the namespace. Inventory
        surface — for "which procedure do I run now," call `.match()`
        instead.

        Returns `{"procedures": [{name, situation, tags, memory_key,
        created_at}]}`.
        """
        return await self._agent.transport.post(
            "/v1/procedure.list",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "agent": self._agent.agent,
                "prefix": prefix,
                "tags": list(tags or []),
                "limit": limit,
            },
        )
