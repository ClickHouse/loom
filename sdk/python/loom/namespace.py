"""Namespace registry client.

await agent.namespaces.register(
    owner_email="ops@example.com",
    description="Detector → validator → reviewer pipeline",
    tags=["pipeline", "detection"],
)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class NamespaceClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def register(
        self,
        *,
        owner_email: str,
        description: str | None = None,
        tags: list[str] | None = None,
        active: bool | None = None,
        namespace: str | None = None,
        conflict_threshold: float | None = None,
        retention_days: int | None = None,
        warm_days: int | None = None,
        visibility: str | None = None,
        rerank_endpoint: str | None = None,
        rerank_prompt_template: str | None = None,
        classify_endpoint: str | None = None,
        classify_prompt_template: str | None = None,
        default_conflict_policy: str | None = None,
        tag_registry: list[dict[str, Any]] | None = None,
        retrieval_defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register this Agent's namespace, or rewrite its row.

        A setting left None keeps the stored value, and a new namespace
        takes the server's default (private, active, no endpoints,
        `auto_reinforce`, the server-wide conflict threshold). `tags` are
        the exception: the list is replaced whole, so pass every tag the
        row should keep. `active=True` reactivates a soft-deleted namespace.
        To return `conflict_threshold`, `capture_generation_io` or
        `coverage_sweep` to the server's default, send an explicit null:
        `agent.call("namespace.register", owner_email=...,
        conflict_threshold=None)`. The settings this
        method does not take (consolidation_enabled, daily_cost_cap_usd,
        requires_approval, join_policy, conversation logging) go through
        `agent.call("namespace.register", ...)` the same way.

        `retrieval_defaults` — what an unpinned memory.search on this
        namespace runs ({search_mode, top_k, auto_touch, include_tags,
        bodies, reinforcement_weight}); {} clears them. `tag_registry` —
        {memory_type, retention_days, dedup_threshold, hot_quota,
        conflict_policy} dicts.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": namespace or self._agent.namespace,
            "owner_email": owner_email,
            "tags": tags or [],
            "tag_registry": tag_registry or [],
        }
        optional = {
            "description": description,
            "active": active,
            "conflict_threshold": conflict_threshold,
            "retention_days": retention_days,
            "warm_days": warm_days,
            "visibility": visibility,
            "rerank_endpoint": rerank_endpoint,
            "rerank_prompt_template": rerank_prompt_template,
            "classify_endpoint": classify_endpoint,
            "classify_prompt_template": classify_prompt_template,
            "default_conflict_policy": default_conflict_policy,
            "retrieval_defaults": retrieval_defaults,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        return await self._agent.transport.post("/v1/namespace.register", payload)

    async def get(self, namespace: str | None = None) -> dict[str, Any] | None:
        result = await self._agent.transport.post(
            "/v1/namespace.get",
            {
                "org": self._agent.org,
                "namespace": namespace or self._agent.namespace,
            },
        )
        if not result.get("found"):
            return None
        return result

    async def validate(
        self,
        *,
        namespace: str | None = None,
        sample_limit: int = 5,
        strict: bool = False,
        conflict_age_seconds: int | None = None,
        confidence_floor: float | None = None,
    ) -> dict[str, Any]:
        """One read-only integrity report for a namespace: per-check counts
        with sample memory_keys, `healthy`, and `skipped` checks. Call it
        after a batch of writes; `strict=True` makes warnings unhealthy."""
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": namespace or self._agent.namespace,
            "sample_limit": sample_limit,
            "strict": strict,
        }
        if conflict_age_seconds is not None:
            payload["conflict_age_seconds"] = conflict_age_seconds
        if confidence_floor is not None:
            payload["confidence_floor"] = confidence_floor
        return await self._agent.transport.post("/v1/namespace.validate", payload)

    async def list(
        self,
        *,
        active_only: bool = True,
        tags: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        result = await self._agent.transport.post(
            "/v1/namespace.list",
            {
                "org": self._agent.org,
                "active_only": active_only,
                "tags": tags or [],
                "limit": limit,
            },
        )
        rows: list[dict[str, Any]] = result["rows"]
        return rows

    async def delete(self, *, namespace: str | None = None) -> dict[str, Any]:
        """Soft-delete this namespace (mark active=false). Data in
        loom.memory / loom.knowledge etc. is NOT removed — issue a
        separate `erasure.request(scope_type='namespace', ...)` first
        if you want the rows gone.

        Idempotent: deleting an already-inactive namespace returns
        `deleted=False`.
        """
        return await self._agent.transport.post(
            "/v1/namespace.delete",
            {
                "org": self._agent.org,
                "namespace": namespace or self._agent.namespace,
            },
        )

    async def register_category(
        self,
        name: str,
        *,
        description: str = "",
        summary: str = "",
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Register a category in the namespace's taxonomy.

        Each category gets its own embedding (computed from name +
        description + summary). Agents can later query the registry
        via `namespaces.search_categories(query)` to find the most
        relevant category before drilling into items via
        `agent.memory.search(category=...)`.

        Re-registering the same name updates description/summary.
        """
        return await self._agent.transport.post(
            "/v1/namespace.register_category",
            {
                "org": self._agent.org,
                "namespace": namespace or self._agent.namespace,
                "name": name,
                "description": description,
                "summary": summary,
            },
        )

    async def list_categories(
        self,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Enumerate registered categories with item counts.

        Returns `{categories: [{category_id, name, description,
        item_count}]}` sorted by item_count desc.
        """
        return await self._agent.transport.post(
            "/v1/namespace.list_categories",
            {
                "org": self._agent.org,
                "namespace": namespace or self._agent.namespace,
            },
        )

    async def search_categories(
        self,
        query: str,
        *,
        top_k: int = 5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Cosine-rank the category registry against the query:
        `{results: [{category_id, name, description, summary_excerpt,
        distance, similarity}]}`, to scope `memory.search(category=...)`."""
        return await self._agent.transport.post(
            "/v1/namespace.search_categories",
            {
                "org": self._agent.org,
                "namespace": namespace or self._agent.namespace,
                "query": query,
                "top_k": int(top_k),
            },
        )

    async def taxonomy(
        self,
        namespace: str | None = None,
        *,
        top_n_tags: int = 50,
        top_n_entities: int = 50,
    ) -> dict[str, Any]:
        """The live vocabulary of a namespace, to reuse its tags and
        entities rather than invent new ones: `{tags, entities,
        memory_types, memory_count}`, each list `[{value, count}, ...]`
        by count, highest first."""
        return await self._agent.transport.post(
            "/v1/namespace.taxonomy",
            {
                "org": self._agent.org,
                "namespace": namespace or self._agent.namespace,
                "top_n_tags": int(top_n_tags),
                "top_n_entities": int(top_n_entities),
            },
        )
