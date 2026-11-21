"""Exact-person selector and membership redaction keeps other administrators' keys."""

from __future__ import annotations

from typing import Any

from server.core.ch import own_write_reads, set_query_tag
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.mutations import count_then_mutate
from server.knowledge.domain.source_crawl import email, without_selectors
from server.settings import settings

_SOURCES = "loom.knowledge_sources"
_GROUPS = "loom.knowledge_group_members"
_SCOPE = "org = {org:String} AND namespace = {ns:String} AND source_id = {id:String}"
_CRAWL_SCOPE = _SCOPE + " AND credential_mode = 'crawl'"


def _params(org: str, namespace: str, source_id: str) -> dict[str, str]:
    if any(not isinstance(v, str) or not v or "\0" in v for v in (org, namespace, source_id)):
        raise ValueError("crawl cleanup scope refused")
    return {"org": org, "ns": namespace, "id": source_id}


async def _history(client: _CHClient, params: dict[str, Any]) -> list[dict[str, str]]:
    maximum = settings.knowledge_crawl_registry_scan_limit
    with own_write_reads():
        rows = await client.query_all(
            f"SELECT DISTINCT options_json, registrant_subject FROM {_SOURCES} "
            f"WHERE {_CRAWL_SCOPE} ORDER BY options_json, registrant_subject "
            "LIMIT {limit:UInt32}",
            params={**params, "limit": maximum + 1},
        )
    if len(rows) > maximum or any(
        not isinstance(r.get(key), str)
        for r in rows
        for key in ("options_json", "registrant_subject")
    ):
        raise RuntimeError("crawl_control_history_unverified")
    return rows


async def _retire(client: _CHClient, where: str, params: dict[str, Any]) -> None:
    # A newer, independently approved configuration of the same source id is not
    # retired merely because an older version named the deleted person.
    await count_then_mutate(
        _SOURCES,
        count_where=where + " AND (active OR approval_enc != '')",
        mutation="UPDATE active = 0, approval_enc = '', crawl_generation = ''",
        mutation_where=where,
        params=params,
        final=False,
        ch=client,
    )


async def controls(
    org: str,
    namespace: str,
    source_id: str,
    subject: str,
    addresses: frozenset[str],
    *,
    spellings: tuple[str, ...] = (),
    ch: _CHClient | None = None,
) -> bool:
    """Caller holds the refusal fence and namespace lease; no new approval is minted."""
    set_query_tag("erasure.crawl_person.controls")
    if not isinstance(subject, str) or not subject or "\0" in subject:
        raise ValueError("canonical crawl subject required")
    addresses = frozenset(email(v) for v in addresses)
    if not addresses:
        raise ValueError("verified crawl contacts required")
    client = ch or _default_client()
    params: dict[str, Any] = {
        **_params(org, namespace, source_id),
        "spellings": sorted({subject, *spellings}),
    }
    rows = await _history(client, params)
    rewrites = {r["options_json"]: without_selectors(r["options_json"], addresses) for r in rows}
    affected = any(before != after for before, after in rewrites.items()) or any(
        row["registrant_subject"] == subject for row in rows
    )
    for before, after in rewrites.items():
        if before != after:
            where = _CRAWL_SCOPE + " AND options_json = {before:String}"
            # The resource hash no longer matches. Keep the shared service-principal
            # key; only a checked administrator can approve this changed registration.
            await _retire(client, where, {**params, "before": before})
            await count_then_mutate(
                _SOURCES,
                count_where=where,
                mutation="UPDATE options_json = {after:String}",
                mutation_where=where,
                params={**params, "before": before, "after": after},
                final=False,
                ch=client,
            )
    for field in ("registrant_subject", "created_by"):
        where = _CRAWL_SCOPE + (
            " AND registrant_subject = {subject:String}"
            if field == "registrant_subject"
            else " AND created_by IN {spellings:Array(String)}"
        )
        if field == "registrant_subject":
            await _retire(client, where, {**params, "subject": subject})
        await count_then_mutate(
            _SOURCES,
            count_where=where,
            mutation=f"UPDATE {field} = ''",
            mutation_where=where,
            params={**params, "subject": subject},
            final=False,
            ch=client,
        )
    if await control_residue(
        org, namespace, source_id, subject, addresses, spellings=spellings, ch=client
    ):
        raise RuntimeError("crawl_control_history_remains")
    return affected


async def control_residue(
    org: str,
    namespace: str,
    source_id: str,
    subject: str,
    addresses: frozenset[str],
    *,
    spellings: tuple[str, ...] = (),
    ch: _CHClient | None = None,
) -> int:
    client, params = ch or _default_client(), _params(org, namespace, source_id)
    rows = await _history(client, params)
    selectors = sum(
        without_selectors(r["options_json"], addresses) != r["options_json"] for r in rows
    )
    with own_write_reads():
        row = await client.query_one(
            f"SELECT count() AS n FROM {_SOURCES} WHERE {_CRAWL_SCOPE} "
            "AND (registrant_subject = {subject:String} "
            "OR created_by IN {spellings:Array(String)})",
            params={**params, "subject": subject, "spellings": sorted({subject, *spellings})},
        )
    if row is None or type(row.get("n")) is not int or row["n"] < 0:
        raise RuntimeError("crawl_control_history_unverified")
    return selectors + int(row["n"])


async def approval_history(
    org: str,
    namespace: str,
    source_id: str,
    *,
    ch: _CHClient | None = None,
) -> list[str]:
    maximum = settings.knowledge_crawl_registry_scan_limit
    with own_write_reads():
        rows = await (ch or _default_client()).query_all(
            f"SELECT DISTINCT approval_enc FROM {_SOURCES} WHERE {_CRAWL_SCOPE} "
            "AND approval_enc != '' ORDER BY approval_enc LIMIT {limit:UInt32}",
            params={**_params(org, namespace, source_id), "limit": maximum + 1},
        )
    if len(rows) > maximum or any(not isinstance(r["approval_enc"], str) for r in rows):
        raise RuntimeError("crawl_approval_history_unverified")
    return [r["approval_enc"] for r in rows]


async def retire_approval(
    org: str,
    namespace: str,
    source_id: str,
    encoded: str,
    *,
    ch: _CHClient | None = None,
) -> None:
    client = ch or _default_client()
    where = _CRAWL_SCOPE + " AND approval_enc = {encoded:String}"
    await _retire(client, where, {**_params(org, namespace, source_id), "encoded": encoded})
    with own_write_reads():
        row = await client.query_one(
            f"SELECT count() AS n FROM {_SOURCES} WHERE {where}",
            params={**_params(org, namespace, source_id), "encoded": encoded},
        )
    if row is None or type(row.get("n")) is not int or row["n"] != 0:
        raise RuntimeError("crawl_approval_history_remains")


async def memberships(
    org: str,
    mirror: str,
    source_id: str,
    addresses: frozenset[str],
    *,
    ch: _CHClient | None = None,
) -> None:
    """All membership versions lose only the requested person's verified contacts."""
    set_query_tag("erasure.crawl_person.memberships")
    client = ch or _default_client()
    params = {**_params(org, mirror, source_id), "emails": sorted(email(v) for v in addresses)}
    if not params["emails"]:
        raise ValueError("verified crawl contacts required")
    where = _SCOPE + " AND hasAny(members, {emails:Array(String)})"
    await count_then_mutate(
        _GROUPS,
        count_where=where,
        mutation=(
            "UPDATE members = arrayFilter(value -> NOT has({emails:Array(String)}, value), members)"
        ),
        mutation_where=where,
        params=params,
        final=False,
        ch=client,
    )
    with own_write_reads():
        row = await client.query_one(
            f"SELECT count() AS n FROM {_GROUPS} WHERE {where}", params=params
        )
    if row is None or type(row.get("n")) is not int or row["n"] != 0:
        raise RuntimeError("crawl_membership_history_remains")


async def invalidate_listing(
    org: str,
    mirror: str,
    source_id: str,
    *,
    ch: _CHClient | None = None,
) -> None:
    client, params = ch or _default_client(), _params(org, mirror, source_id)
    table = "loom.knowledge_source_listings"
    await count_then_mutate(
        table,
        count_where=_SCOPE,
        mutation="DELETE",
        mutation_where=_SCOPE,
        params=params,
        final=False,
        ch=client,
    )
    with own_write_reads():
        row = await client.query_one(
            f"SELECT count() AS n FROM {table} WHERE {_SCOPE}", params=params
        )
    if row is None or type(row.get("n")) is not int or row["n"] != 0:
        raise RuntimeError("crawl_listing_history_remains")


async def capsules(uid: str, *, ch: _CHClient | None = None) -> None:
    """Under the exact SCIM user lease, remove every completed capture's encrypted PII."""
    if not isinstance(uid, str) or not uid or "\0" in uid:
        raise ValueError("SCIM capture scope refused")
    client, params = ch or _default_client(), {"id": uid}
    where = "id = {id:String} AND (crawl_erasure_enc != '' OR NOT crawl_erasure_done)"
    await count_then_mutate(
        "loom.scim_users",
        count_where=where,
        mutation_where=where,
        final=False,
        mutation=(
            "UPDATE crawl_erasure_enc = '', crawl_erasure_done = true, "
            "crawl_erasure_required = true"
        ),
        params=params,
        ch=client,
    )
    with own_write_reads():
        row = await client.query_one(
            f"SELECT count() AS n FROM loom.scim_users WHERE {where}", params=params
        )
    if row is None or type(row.get("n")) is not int or row["n"] != 0:
        raise RuntimeError("crawl_capture_history_remains")


async def capsule_history(uid: str, *, ch: _CHClient | None = None) -> list[str]:
    if not isinstance(uid, str) or not uid or "\0" in uid:
        raise ValueError("SCIM capture scope refused")
    maximum = settings.knowledge_crawl_registry_scan_limit
    with own_write_reads():
        rows = await (ch or _default_client()).query_all(
            "SELECT DISTINCT crawl_erasure_enc FROM loom.scim_users "
            "WHERE id = {id:String} AND crawl_erasure_enc != '' "
            "ORDER BY crawl_erasure_enc LIMIT {limit:UInt32}",
            params={"id": uid, "limit": maximum + 1},
        )
    if len(rows) > maximum or any(not isinstance(r["crawl_erasure_enc"], str) for r in rows):
        raise RuntimeError("crawl_capture_history_unverified")
    return [r["crawl_erasure_enc"] for r in rows]


async def invalidate_restored_approvals(org: str, *, ch: _CHClient) -> None:
    """A restore never reactivates historical delegated source authority."""
    where = "org = {org:String} AND credential_mode = 'crawl'"
    await count_then_mutate(
        _SOURCES,
        count_where=where + " AND (active OR approval_enc != '' OR crawl_generation != '')",
        mutation="UPDATE active = 0, approval_enc = '', crawl_generation = ''",
        mutation_where=where,
        params={"org": org},
        final=False,
        ch=ch,
    )
    residue = await ch.query_one(
        f"SELECT count() AS n FROM {_SOURCES} WHERE {where} "
        "AND (active OR approval_enc != '' OR crawl_generation != '')",
        params={"org": org},
    )
    if residue is None or residue["n"]:
        raise RuntimeError("crawl_restore_approval_residue")
