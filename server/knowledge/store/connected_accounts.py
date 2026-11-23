"""The current connected registrations and physical erasure of their historical rows."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from server.core.ch import own_write_reads
from server.core.grant_sql import LIVE_GRANT
from server.core.repo_base import _CHClient, _default_client
from server.core.versions import next_version
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.settings import settings

TABLE = "loom.connected_accounts"
_COLUMNS = (
    "org, subject, kind, connection_id, account_id, workspace_id, account_email_enc, "
    "credential_enc, scopes, active, refresh_id, connected_at, disconnected_at, version"
)


def _account(row: dict[str, Any]) -> ConnectedAccount:
    return ConnectedAccount(
        org=row["org"],
        subject=row["subject"],
        kind=row["kind"],
        connection_id=row["connection_id"],
        account_id=row.get("account_id", ""),
        workspace_id=row.get("workspace_id", ""),
        account_email_enc=row.get("account_email_enc", ""),
        credential_enc=row.get("credential_enc", ""),
        scopes=tuple(row.get("scopes") or ()),
        active=row.get("active", False),
        refresh_id=row.get("refresh_id", ""),
        connected_at=row.get("connected_at"),
        disconnected_at=row.get("disconnected_at"),
        version=row.get("version", 0),
    )


async def get(
    org: str, subject: str, kind: str, *, ch: _CHClient | None = None
) -> ConnectedAccount | None:
    with own_write_reads():
        row = await (ch or _default_client()).query_one(
            f"SELECT {_COLUMNS} FROM {TABLE} FINAL WHERE org = {{org:String}} "
            "AND subject = {subject:String} AND kind = {kind:String} LIMIT 1",
            params={"org": org, "subject": subject, "kind": kind},
        )
    return _account(dict(row)) if row else None


async def owned(
    subjects: list[str], *, org: str | None = None, ch: _CHClient | None = None
) -> list[ConnectedAccount]:
    if not subjects:
        return []
    where = "subject IN {subjects:Array(String)}"
    params: dict[str, Any] = {"subjects": subjects}
    if org is not None:
        where = "org = {org:String} AND " + where
        params["org"] = org
    limit = settings.knowledge_connected_registry_scan_limit
    with own_write_reads():
        rows = await (ch or _default_client()).query_all(
            f"SELECT {_COLUMNS} FROM {TABLE} FINAL WHERE {where} "
            "ORDER BY org, subject, kind LIMIT {limit:UInt64}",
            params={**params, "limit": limit + 1},
        )
    if len(rows) > limit:
        raise RuntimeError("connected_account_owner_bound")
    return [_account(dict(row)) for row in rows]


async def put(account: ConnectedAccount, *, ch: _CHClient | None = None) -> None:
    """The caller holds this account's lease from its latest read through this insert."""
    client = ch or _default_client()
    row = asdict(account)
    row["scopes"] = list(account.scopes)
    row["version"] = await next_version(
        client,
        TABLE,
        {
            "org": account.org,
            "subject": account.subject,
            "kind": account.kind,
        },
    )
    await client.insert(TABLE, [row])


async def owner_identities(
    *, org: str | None = None, ch: _CHClient | None = None
) -> list[tuple[str, str]]:
    """Lifecycle revocation discovers verified aliases without reading sealed payloads."""
    where = "WHERE org = {org:String} " if org is not None else ""
    maximum = settings.knowledge_connected_registry_scan_limit
    with own_write_reads():
        rows = await (ch or _default_client()).query_all(
            f"SELECT DISTINCT org, subject FROM {TABLE} FINAL {where}"
            "ORDER BY org, subject LIMIT {limit:UInt64}",
            params={"org": org or "", "limit": maximum + 1},
        )
    if len(rows) > maximum:
        raise RuntimeError("connected_account_owner_bound")
    return [(str(row["org"]), str(row["subject"])) for row in rows]


async def erase(
    org: str,
    subjects: list[str],
    *,
    kind: str,
    ch: _CHClient | None = None,
) -> None:
    """Delete every historical secret/identity version after writers have been fenced."""
    if subjects:
        await (ch or _default_client()).execute(
            f"DELETE FROM {TABLE} WHERE org = {{org:String}} "
            "AND subject IN {subjects:Array(String)} AND kind = {kind:String}",
            params={"org": org, "subjects": subjects, "kind": kind},
            settings={"lightweight_deletes_sync": 2},
        )


async def account_active(org: str, subjects: list[str], *, ch: _CHClient | None = None) -> bool:
    """The existing delegation standing rule, read uncached for credential checkout."""
    with own_write_reads():
        row = await (ch or _default_client()).query_one(
            f"""
            SELECT o.owned AS owned, o.live AS owned_live, g.granted AS granted,
                   g.live AS granted_live
            FROM (
                SELECT count() AS owned, countIf(active = 1) AS live
                FROM loom.namespaces FINAL
                WHERE org = {{org:String}} AND lower(owner_email) IN {{subjects:Array(String)}}
            ) AS o CROSS JOIN (
                SELECT countIf(status = 'active') AS granted, countIf({LIVE_GRANT}) AS live
                FROM loom.namespace_grants FINAL
                WHERE org = {{org:String}} AND lower(subject) IN {{subjects:Array(String)}}
            ) AS g
            """,
            params={"org": org, "subjects": [s.lower() for s in subjects]},
        )
    if row is None:
        return False
    # An operator with neither registration nor grant is not a disabled account.
    return bool(row["owned_live"] or row["granted_live"]) or not bool(
        row["owned"] or row["granted"]
    )


async def page(
    after: tuple[str, str, str], *, ch: _CHClient | None = None
) -> list[ConnectedAccount]:
    """Bounded discovery for resealing; writers re-read each key under its own lease."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE (org, subject, kind) > {after:Tuple(String, String, String)} "
        "AND (credential_enc != '' OR account_email_enc != '') "
        "ORDER BY org, subject, kind LIMIT {limit:UInt32}",
        params={"after": after, "limit": settings.knowledge_connected_registry_page_size},
    )
    return [_account(dict(row)) for row in rows]


async def restore_keys(
    *, owner: tuple[str, str, str] | None = None, ch: _CHClient | None = None
) -> list[tuple[str, str, str, str]]:
    """Every historical registration key, without selecting any sealed payload."""
    where = "WHERE (org, subject, kind) = {owner:Tuple(String, String, String)} " if owner else ""
    maximum = settings.knowledge_connected_registry_scan_limit
    rows = await (ch or _default_client()).query_all(
        f"SELECT DISTINCT org, subject, kind, connection_id FROM {TABLE} {where}"
        "ORDER BY org, subject, kind, connection_id LIMIT {limit:UInt64}",
        params={"owner": owner or ("", "", ""), "limit": maximum + 1},
    )
    if len(rows) > maximum:
        raise RuntimeError("connected_restore_inventory_bound")
    return [(row["org"], row["subject"], row["kind"], row["connection_id"]) for row in rows]


async def restore_workers_recent(*, ch: _CHClient | None = None) -> int:
    row = await (ch or _default_client()).query_one(
        "SELECT count() AS n FROM loom.worker_status "
        "WHERE pass_finished > now64(3) - toIntervalSecond({s:UInt32})",
        params={"s": 2 * settings.supervisor_lease_ttl_s},
    )
    if row is None or "n" not in row:
        raise RuntimeError("connected_restore_offline_unavailable")
    return int(row["n"])


async def mcp_owner_page(
    org: str, subject: str, *, after: str = "", limit: int = 101, ch: _CHClient | None = None
) -> list[ConnectedAccount]:
    """Current valid MCP slots for one canonical owner, including stopped markers."""
    if type(limit) is not int or not 1 <= limit <= 101:
        raise ValueError("mcp_account_page_bound")
    with own_write_reads():
        rows = await (ch or _default_client()).query_all(
            f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
            "WHERE org = {org:String} AND subject = {subject:String} "
            "AND match(kind, {slot_pattern:String}) AND kind > {after:String} "
            "ORDER BY kind LIMIT {limit:UInt32}",
            params={
                "org": org,
                "subject": subject,
                "slot_pattern": r"^mcp:[0-9a-f]{32}$",
                "after": after,
                "limit": limit,
            },
        )
    return [_account(dict(row)) for row in rows]


async def mcp_owner_active_count(
    org: str, subject: str, *, maximum: int, ch: _CHClient | None = None
) -> int:
    """Count only to the quota ceiling plus one; the caller holds its owner barrier."""
    if type(maximum) is not int or not 1 <= maximum <= 4096:
        raise ValueError("mcp_account_count_bound")
    with own_write_reads():
        row = await (ch or _default_client()).query_one(
            f"SELECT count() AS n FROM (SELECT 1 FROM {TABLE} FINAL "
            "WHERE org = {org:String} AND subject = {subject:String} AND active = 1 "
            "AND match(kind, {slot_pattern:String}) LIMIT {limit:UInt32})",
            params={
                "org": org,
                "subject": subject,
                "slot_pattern": r"^mcp:[0-9a-f]{32}$",
                "limit": maximum + 1,
            },
        )
    if row is None:
        raise RuntimeError("mcp_account_count_unavailable")
    return int(row["n"])
