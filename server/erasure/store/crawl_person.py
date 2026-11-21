"""Historical crawl ACL redaction preserves document text and other readers' grants."""

from __future__ import annotations

from typing import Any

from server.core import pg
from server.core.ch import own_write_reads, set_query_tag
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import count_then_mutate
from server.knowledge.domain.source_crawl import email, without_people
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.settings import settings

_CH_SCOPE = (
    "org = {org:String} AND namespace = {ns:String} "
    "AND actor = {actor:String} AND has(tags, {tag:String})"
)
_PG_SCOPE = "org = $1 AND namespace = $2 AND row->>'actor' = $3 AND row->'tags' ? $4::text"


def _params(org: str, namespace: str, source_id: str) -> dict[str, str]:
    if any(not isinstance(v, str) or not v or "\0" in v for v in (org, namespace, source_id)):
        raise ValueError("crawl cleanup scope refused")
    return {
        "org": org,
        "ns": namespace,
        "actor": SYNC_WORKER_ACTOR,
        "tag": "source-id:" + source_id,
    }


async def _pg_values(conn: Any, args: list[str]) -> list[str]:
    maximum = settings.knowledge_crawl_registry_scan_limit
    rows = await conn.fetch(
        "SELECT DISTINCT row->>'access_json' AS access_json FROM ("
        f"SELECT row FROM loom.knowledge_heads WHERE {_PG_SCOPE} "
        f"UNION ALL SELECT row FROM loom.knowledge_deliveries WHERE {_PG_SCOPE}"
        ") AS copies ORDER BY access_json LIMIT $5",
        *args,
        maximum + 1,
    )
    if len(rows) > maximum or any(
        r["access_json"] is not None and not isinstance(r["access_json"], str) for r in rows
    ):
        raise RuntimeError("crawl_permission_history_bound")
    return [r["access_json"] or "" for r in rows]


async def _pg_scrub(args: list[str], addresses: frozenset[str]) -> None:
    if not pg.enabled():
        return
    async with pg.transaction() as conn:
        await conn.execute(f"SELECT pg_advisory_xact_lock({pg.KNOWLEDGE_LOCK_KEY})", *args[:2])
        await conn.execute(
            f"SELECT 1 FROM loom.knowledge_heads WHERE {_PG_SCOPE} "
            "ORDER BY org, namespace, source_uri FOR UPDATE",
            *args,
        )
        held = await conn.fetch(
            f"SELECT {pg.LEASE_LEFT_S} AS left_s FROM loom.knowledge_deliveries d "
            f"WHERE {_PG_SCOPE} FOR UPDATE",
            *args,
        )
        # Inspect every retained delivery, including already-redacted retries. Its stored
        # fence keeps an earlier attempt's old publisher covered after a crash.
        left = [float(row["left_s"]) for row in held]
        for before in await _pg_values(conn, args):
            after = without_people(before, addresses)
            if after == before:
                continue
            await conn.execute(
                "UPDATE loom.knowledge_heads SET row = jsonb_set(row, '{access_json}', "
                f"to_jsonb($6::text), true) WHERE {_PG_SCOPE} "
                "AND coalesce(row->>'access_json', '') = $5",
                *args,
                before,
                after,
            )
            await conn.execute(
                "UPDATE loom.knowledge_deliveries SET row = jsonb_set(row, '{access_json}', "
                "to_jsonb($6::text), true), lease_token = lease_token + 1, "
                "fenced_until = GREATEST(fenced_until, CASE WHEN isfinite(lease_until) "
                "THEN lease_until ELSE fenced_until END) "
                f"WHERE {_PG_SCOPE} AND coalesce(row->>'access_json', '') = $5",
                *args,
                before,
                after,
            )
    # Existing in-memory publishers may still send the old row until their lease ends.
    await pg.wait_out(left)


async def _ch_values(client: _CHClient, params: dict[str, str]) -> list[str]:
    maximum = settings.knowledge_crawl_registry_scan_limit
    with own_write_reads():
        rows = await client.query_all(
            f"SELECT DISTINCT access_json FROM loom.documents WHERE {_CH_SCOPE} "
            "ORDER BY access_json LIMIT {limit:UInt32}",
            params={**params, "limit": maximum + 1},
        )
    if len(rows) > maximum or any(not isinstance(row["access_json"], str) for row in rows):
        raise RuntimeError("crawl_permission_history_bound")
    return [row["access_json"] for row in rows]


async def scrub(
    org: str,
    namespace: str,
    source_id: str,
    addresses: frozenset[str],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Caller holds its checked person-erasure fence and exact org→source leases."""
    set_query_tag("erasure.crawl_person.acl")
    params = _params(org, namespace, source_id)
    addresses = frozenset(email(value) for value in addresses)
    if not addresses:
        raise ValueError("crawl cleanup contacts required")
    await _pg_scrub(list(params.values()), addresses)
    client = ch or _default_client()
    for before in await _ch_values(client, params):
        after = without_people(before, addresses)
        if after == before:
            continue
        where = _CH_SCOPE + " AND access_json = {before:String}"
        await count_then_mutate(
            "loom.documents",
            count_where=where,
            final=False,
            mutation="UPDATE access_json = {after:String}",
            mutation_where=where,
            params={**params, "before": before, "after": after},
            ch=client,
        )
    if await residue(org, namespace, source_id, addresses, ch=client):
        raise RuntimeError("crawl_permission_history_remains")


async def residue(
    org: str,
    namespace: str,
    source_id: str,
    addresses: frozenset[str],
    *,
    ch: _CHClient | None = None,
) -> int:
    """Raw history in both authorities, not FINAL-only positive serving rows."""
    params = _params(org, namespace, source_id)
    total = 0
    for value in await _ch_values(ch or _default_client(), params):
        total += without_people(value, addresses) != value
    if pg.enabled():
        async with pg.transaction() as conn:
            for value in await _pg_values(conn, list(params.values())):
                total += without_people(value, addresses) != value
    return total


async def namespaces(org: str, source_id: str, *, ch: _CHClient | None = None) -> list[str]:
    """Discover raw retained copies, including older mirrors and pending PG deliveries."""
    if any(not isinstance(v, str) or not v or "\0" in v for v in (org, source_id)):
        raise ValueError("crawl copy scope refused")
    client = ch or _default_client()
    maximum = settings.knowledge_crawl_registry_scan_limit
    params = {
        "org": org,
        "id": source_id,
        "actor": SYNC_WORKER_ACTOR,
        "tag": "source-id:" + source_id,
        "limit": maximum + 1,
    }
    with own_write_reads():
        rows = await client.query_all(
            "SELECT DISTINCT namespace FROM ("
            "SELECT namespace FROM loom.documents WHERE org = {org:String} "
            "AND actor = {actor:String} AND has(tags, {tag:String}) UNION ALL "
            "SELECT namespace FROM loom.knowledge_group_members WHERE org = {org:String} "
            "AND source_id = {id:String} UNION ALL "
            "SELECT namespace FROM loom.knowledge_source_listings WHERE org = {org:String} "
            "AND source_id = {id:String}) ORDER BY namespace LIMIT {limit:UInt32}",
            params=params,
        )
    if pg.enabled():
        async with pg.transaction() as conn:
            rows.extend(
                await conn.fetch(
                    "SELECT DISTINCT namespace FROM ("
                    "SELECT namespace FROM loom.knowledge_heads WHERE org = $1 "
                    "AND row->>'actor' = $2 AND row->'tags' ? $3::text UNION ALL "
                    "SELECT namespace FROM loom.knowledge_deliveries WHERE org = $1 "
                    "AND row->>'actor' = $2 AND row->'tags' ? $3::text) AS copies "
                    "ORDER BY namespace LIMIT $4",
                    org,
                    SYNC_WORKER_ACTOR,
                    params["tag"],
                    maximum + 1,
                )
            )
    if len(rows) > 2 * maximum or any(
        not isinstance(r["namespace"], str) or not r["namespace"] or "\0" in r["namespace"]
        for r in rows
    ):
        raise RuntimeError("crawl_copy_scope_unverified")
    result = sorted({r["namespace"] for r in rows})
    if len(result) > maximum:
        raise RuntimeError("crawl_copy_scope_unverified")
    return result
