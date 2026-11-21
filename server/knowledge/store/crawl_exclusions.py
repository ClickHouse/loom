"""Org-scoped refusal identities; no raw provider id or address is stored."""

from __future__ import annotations

from typing import Any

from server.core.ch import own_write_reads
from server.core.repo_base import _CHClient, _default_client
from server.core.versions import next_version

KEYS = "loom.knowledge_crawl_exclusion_keys"
REFUSALS = "loom.knowledge_crawl_exclusions"


async def key(org: str, *, ch: _CHClient | None = None) -> dict[str, Any] | None:
    with own_write_reads():
        return await (ch or _default_client()).query_one(
            "SELECT key_id, key_enc, generation, pending_id, pending_digests, "
            "pending_exclude, version "
            f"FROM {KEYS} FINAL "
            "WHERE org = {org:String} LIMIT 1",
            params={"org": org},
        )


async def key_put(
    org: str,
    key_id: str,
    key_enc: str,
    *,
    generation: str,
    pending_id: str = "",
    pending_digests: tuple[str, ...] = (),
    pending_exclude: bool = False,
    ch: _CHClient | None = None,
) -> None:
    client = ch or _default_client()
    await client.insert(
        KEYS,
        [
            {
                "org": org,
                "key_id": key_id,
                "key_enc": key_enc,
                "generation": generation,
                "pending_id": pending_id,
                "pending_digests": list(pending_digests),
                "pending_exclude": pending_exclude,
                "version": await next_version(client, KEYS, {"org": org}),
            }
        ],
    )


async def any_refusal(org: str, *, ch: _CHClient | None = None) -> bool:
    with own_write_reads():
        return (
            await (ch or _default_client()).query_one(
                f"SELECT digest FROM {REFUSALS} FINAL WHERE org = {{org:String}} LIMIT 1",
                params={"org": org},
            )
            is not None
        )


async def excluded(org: str, digests: list[str], *, ch: _CHClient | None = None) -> set[str]:
    if not digests:
        return set()
    with own_write_reads():
        rows = await (ch or _default_client()).query_all(
            f"SELECT digest FROM {REFUSALS} FINAL WHERE org = {{org:String}} "
            "AND digest IN {digests:Array(String)} AND excluded = 1 LIMIT {limit:UInt32}",
            params={"org": org, "digests": digests, "limit": len(digests)},
        )
    found = {str(row["digest"]) for row in rows}
    if len(rows) != len(found) or not found.issubset(digests):
        raise RuntimeError("crawl_exclusions_unverified")
    return found


async def put(org: str, digests: list[str], *, exclude: bool, ch: _CHClient | None = None) -> None:
    if digests:
        client = ch or _default_client()
        await client.insert(
            REFUSALS,
            [
                {
                    "org": org,
                    "digest": digest,
                    "excluded": exclude,
                    "version": await next_version(client, REFUSALS, {"org": org, "digest": digest}),
                }
                for digest in sorted(set(digests))
            ],
        )


async def page(after: str, maximum: int, *, ch: _CHClient | None = None) -> list[str]:
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("crawl registry page bound refused")
    with own_write_reads():
        rows = await (ch or _default_client()).query_all(
            f"SELECT org FROM {KEYS} FINAL WHERE org > {{after:String}} "
            "ORDER BY org LIMIT {limit:UInt32}",
            params={"after": after, "limit": maximum},
        )
    orgs = [str(row["org"]) for row in rows]
    if len(orgs) > maximum or orgs != sorted(set(orgs)) or any(org <= after for org in orgs):
        raise RuntimeError("crawl_registry_page_unverified")
    return orgs


async def restore_orgs(*, maximum: int, ch: _CHClient) -> list[str]:
    with own_write_reads():
        rows = await ch.query_all(
            "SELECT DISTINCT org FROM (SELECT org FROM loom.knowledge_sources "
            "WHERE credential_mode = 'crawl' UNION ALL "
            "SELECT org FROM loom.knowledge_crawl_exclusion_keys) "
            "ORDER BY org LIMIT {limit:UInt32}",
            params={"limit": maximum + 1},
        )
    orgs = [row["org"] for row in rows]
    if (
        maximum <= 0
        or len(orgs) > maximum
        or orgs != sorted(set(orgs))
        or any(not isinstance(org, str) or not org or "\0" in org for org in orgs)
    ):
        raise RuntimeError("crawl_restore_inventory_unverified")
    return orgs
