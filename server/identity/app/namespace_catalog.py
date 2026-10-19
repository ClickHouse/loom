"""A namespace's vocabulary: its de-facto taxonomy (tags and entities across
its memories, cached per version and published for the dashboard) and its
registered categories."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from server.core.ch import get_client
from server.identity.api.models_namespace_catalog import (
    NamespaceListCategoriesInput,
    NamespaceListCategoriesOutput,
    NamespaceRegisterCategoryInput,
    NamespaceRegisterCategoryOutput,
    NamespaceSearchCategoriesInput,
    NamespaceSearchCategoriesOutput,
    NamespaceTaxonomyInput,
    NamespaceTaxonomyOutput,
    _CategoryEntry,
    _CategoryHit,
    _TaxonomyCommunity,
    _TaxonomyEntry,
)

# A taxonomy is four FINAL scans of the namespace (~20 MB per Graph open on a large
# namespace) and changes only when the namespace does. It is remembered per (org, namespace,
# namespace version, top-N arguments) for up to five minutes — the version watermark is
# `max(version)` over the namespace, one 90 kB read; the five minutes bound the communities,
# which come from their own table.
_TAXONOMY_TTL_S = 300.0
_taxonomy_cache: dict[tuple[Any, ...], tuple[float, NamespaceTaxonomyOutput]] = {}


def reset_taxonomy_cache() -> None:
    """Tests."""
    _taxonomy_cache.clear()


async def _taxonomy_version(ch: Any, org: str, namespace: str) -> int | None:
    """The namespace's newest memory version, or None when the client cannot
    say (a fake without `query_one`, a failed read) — then nothing is
    remembered."""
    query_one = getattr(ch, "query_one", None)
    if query_one is None:
        return None
    try:
        row = await query_one(
            "SELECT max(version) AS v FROM loom.memory "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": org, "ns": namespace},
        )
    except Exception:  # noqa: BLE001 — the memo is an optimisation
        return None
    v = (row or {}).get("v")
    return int(v) if v else None


async def handle_namespace_taxonomy(
    input: NamespaceTaxonomyInput,
) -> NamespaceTaxonomyOutput:
    """Aggregate tag / entity / type frequencies for one namespace."""

    ch = get_client()
    nsv = await _taxonomy_version(ch, input.org, input.namespace)
    memo_key = (
        input.org,
        input.namespace,
        nsv,
        int(input.top_n_tags),
        int(input.top_n_entities),
        int(input.top_n_communities),
    )
    if nsv is not None:
        hit = _taxonomy_cache.get(memo_key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1].model_copy(deep=True)
    # The memo is per process, and the dashboard's Graph opens land on several. A state row
    # keyed by the namespace version holds the answer the first process computed
    # (7 statements, 9.4 MB, 111 ms) so the others read 20 kB instead. Only a caller that
    # looked and found no row computes and publishes.
    published = await _published_taxonomy(input, nsv) if nsv is not None else None
    out = published if published is not None else await _compute_taxonomy(ch, input)
    if nsv is not None:
        if len(_taxonomy_cache) > 512:
            _taxonomy_cache.clear()
        _taxonomy_cache[memo_key] = (time.monotonic() + _TAXONOMY_TTL_S, out.model_copy(deep=True))
        if published is None:
            await _publish_taxonomy(input, nsv, out)
    return out


def taxonomy_state_key(input: NamespaceTaxonomyInput, nsv: int) -> str:
    """The `code_index_state` key of one namespace version's taxonomy answer for one shape of
    request."""
    return (
        f"taxonomy@{int(nsv)}:{int(input.top_n_tags)}:{int(input.top_n_entities)}"
        f":{int(input.top_n_communities)}"
    )


async def _published_taxonomy(
    input: NamespaceTaxonomyInput, nsv: int
) -> NamespaceTaxonomyOutput | None:
    from server.code.app.reads import code_index

    try:
        repo = code_index()  # a disconnected client raises here; no row is no row
        raw = await repo.get_state(input.org, input.namespace, taxonomy_state_key(input, nsv))
    except Exception as e:  # noqa: BLE001 — no row is no row
        logging.getLogger(__name__).debug("namespace.taxonomy: published read failed: %s", e)
        return None
    if not raw:
        return None
    try:
        return NamespaceTaxonomyOutput.model_validate_json(raw)
    except Exception:  # noqa: BLE001 — a row that does not parse is a missing row
        return None


async def _publish_taxonomy(
    input: NamespaceTaxonomyInput, nsv: int, out: NamespaceTaxonomyOutput
) -> None:
    from server.code.app.reads import code_index

    try:
        repo = code_index()
        await repo.set_state(
            input.org,
            input.namespace,
            taxonomy_state_key(input, nsv),
            out.model_dump_json(),
            detail="taxonomy view shared across processes",
        )
    except Exception as e:  # noqa: BLE001 — a failed publish keeps this process's memo
        logging.getLogger(__name__).debug("namespace.taxonomy: publish failed: %s", e)


async def _compute_taxonomy(ch: Any, input: NamespaceTaxonomyInput) -> NamespaceTaxonomyOutput:
    """The four scans and the community lookup, uncached."""
    from server.memory.domain.entity_sentinels import SENTINEL_ENTITIES

    # One round-trip: top-N tag/entity/type + total via UNION ALL, with
    # a `kind` discriminator column.
    sql = """
        SELECT 'tag' AS kind, value, total AS count FROM (
            SELECT arrayJoin(tags) AS value, count() AS total
            FROM loom.memory FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND length(tags) > 0
            GROUP BY value
            ORDER BY total DESC
            LIMIT {ntag:UInt32}
        )
        UNION ALL
        SELECT 'entity' AS kind, value, total AS count FROM (
            -- Write-path sentinels ("_none_", "_parse_failed_") are not
            -- entities; the graph page seeds its walk from this list.
            SELECT arrayJoin(arrayFilter(x -> NOT has({sent:Array(String)}, x), entities)) AS value,
                   count() AS total
            FROM loom.memory FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND length(entities) > 0
              -- a 'high' row's entities may be mined from its value
              AND sensitivity != 'high'
            GROUP BY value
            ORDER BY total DESC
            LIMIT {nent:UInt32}
        )
        UNION ALL
        SELECT 'type' AS kind, value, total AS count FROM (
            SELECT toString(memory_type) AS value, count() AS total
            FROM loom.memory FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            GROUP BY value
            ORDER BY total DESC
        )
        UNION ALL
        SELECT 'count' AS kind, '' AS value, count() AS total
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND tombstoned_at IS NULL
    """
    rows = await ch.query_all(
        sql,
        params={
            "org": input.org,
            "ns": input.namespace,
            "ntag": int(input.top_n_tags),
            "nent": int(input.top_n_entities),
            "sent": sorted(SENTINEL_ENTITIES),
        },
    )

    tags: list[_TaxonomyEntry] = []
    entities: list[_TaxonomyEntry] = []
    types: list[_TaxonomyEntry] = []
    memory_count = 0
    for r in rows:
        kind = r["kind"]
        if kind == "tag":
            tags.append(_TaxonomyEntry(value=r["value"], count=int(r["count"])))
        elif kind == "entity":
            entities.append(_TaxonomyEntry(value=r["value"], count=int(r["count"])))
        elif kind == "type":
            types.append(_TaxonomyEntry(value=r["value"], count=int(r["count"])))
        elif kind == "count":
            memory_count = int(r["count"])

    communities: list[_TaxonomyCommunity] = []
    computed_at = ""
    if int(input.top_n_communities) > 0:
        try:
            from server.memory.app.entity_communities import fetch_communities

            found = await fetch_communities(
                ch,
                org=input.org,
                namespace=input.namespace,
                top_n=int(input.top_n_communities),
            )
            for c in found:
                communities.append(
                    _TaxonomyCommunity(
                        community_id=c["community_id"],
                        label=c["label"],
                        size=c["size"],
                        top_entities=c["top_entities"],
                    )
                )
            if found and found[0].get("computed_at"):
                computed_at = str(found[0]["computed_at"])
        except Exception as e:  # noqa: BLE001 — communities are additive
            import logging

            logging.getLogger(__name__).warning(
                "namespace.taxonomy: community lookup failed: %s", e
            )

    return NamespaceTaxonomyOutput(
        tags=tags,
        entities=entities,
        memory_types=types,
        memory_count=memory_count,
        communities=communities,
        communities_computed_at=computed_at,
    )


async def handle_namespace_register_category(
    input: NamespaceRegisterCategoryInput,
) -> NamespaceRegisterCategoryOutput:
    import hashlib

    from server.embedding import get_embedder

    ch = get_client()
    embedder = get_embedder()
    # Embed description + summary together for a fuller search signal.
    text = (
        f"{input.name}. {input.description}. {input.summary}"
        if (input.description or input.summary)
        else input.name
    )
    embedding: list[float] = []
    embedded = False
    try:
        embedding = await embedder.embed(text)
        embedded = True
    except Exception:  # noqa: BLE001 — embed failure registers the category unembedded
        pass
    # Deterministic id so re-registrations don't multiply rows.
    cat_id = hashlib.sha256(
        f"{input.org}\x00{input.namespace}\x00{input.name}".encode()
    ).hexdigest()[:32]
    now = datetime.now(UTC).replace(tzinfo=None)
    # Nanoseconds, as the clickgap import stamps the same deterministic row: a
    # millisecond version would lose to any imported row under FINAL.
    version = time.time_ns()
    await ch.insert(
        "loom.categories",
        [
            {
                "org": input.org,
                "namespace": input.namespace,
                "category_id": cat_id,
                "name": input.name,
                "description": input.description,
                "summary": input.summary,
                "embedding": embedding or [],
                "embedding_model": embedder.model_name if embedded else "",
                "embedding_dim": len(embedding) if embedded else 0,
                "version": version,
                "created_at": now,
                "tombstoned_at": None,
            }
        ],
    )
    return NamespaceRegisterCategoryOutput(category_id=cat_id, embedded=embedded)


async def _category_item_counts(org: str, namespace: str) -> dict[str, int]:
    """Live memory count per category, one grouped read over the link table."""
    rows = await get_client().query_all(
        """
        SELECT category_id, count() AS item_count
        FROM loom.memory_categories FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND tombstoned_at IS NULL
        GROUP BY category_id
        """,
        params={"org": org, "ns": namespace},
    )
    return {str(r["category_id"]): int(r["item_count"]) for r in rows}


async def handle_namespace_list_categories(
    input: NamespaceListCategoriesInput,
) -> NamespaceListCategoriesOutput:
    # Two plain reads; the join and the ordering happen here, because a correlated subquery
    # named in ORDER BY is rejected by ClickHouse 26.8 (code 48).
    rows = await get_client().query_all(
        """
        SELECT category_id, name, description
        FROM loom.categories FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND tombstoned_at IS NULL
        """,
        params={"org": input.org, "ns": input.namespace},
    )
    counts = await _category_item_counts(input.org, input.namespace) if rows else {}
    entries = [
        _CategoryEntry(
            category_id=str(r["category_id"]),
            name=str(r["name"]),
            description=str(r["description"]),
            item_count=counts.get(str(r["category_id"]), 0),
        )
        for r in rows
    ]
    entries.sort(key=lambda e: (-e.item_count, e.name))
    return NamespaceListCategoriesOutput(categories=entries)


async def handle_namespace_search_categories(
    input: NamespaceSearchCategoriesInput,
) -> NamespaceSearchCategoriesOutput:
    from server.embedding import get_embedder

    embedder = get_embedder()
    try:
        q_vec = await embedder.embed(input.query)
    except Exception:  # noqa: BLE001 — embed failure answers no categories
        return NamespaceSearchCategoriesOutput(results=[])

    rows = await get_client().query_all(
        """
        SELECT
            category_id,
            name,
            description,
            substring(summary, 1, 280) AS summary_excerpt,
            cosineDistance(embedding, {q:Array(Float32)}) AS distance
        FROM loom.categories FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND embedding_dim > 0
          AND tombstoned_at IS NULL
        ORDER BY distance ASC
        LIMIT {top_k:UInt32}
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "q": q_vec,
            "top_k": int(input.top_k),
        },
    )
    return NamespaceSearchCategoriesOutput(
        results=[
            _CategoryHit(
                category_id=r["category_id"],
                name=r["name"],
                description=r["description"],
                summary_excerpt=r["summary_excerpt"],
                distance=float(r["distance"]),
                similarity=1.0 - float(r["distance"]),
            )
            for r in rows
        ]
    )
