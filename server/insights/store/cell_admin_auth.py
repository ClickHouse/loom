"""Bounded point reads for one cell administrator's security state."""

from __future__ import annotations

from collections import Counter
from typing import Any

from server.core.ch import get_client
from server.core.ch_bounded import BoundedReadError, read_rows


async def machine(name: str, *, deadline: float, ch: Any = None) -> dict[str, Any] | None:
    """Read only the named machine, including disabled state; never enumerate bots."""
    if not name or len(name.encode("utf-8")) > 256 or "\0" in name:
        raise BoundedReadError("Invalid machine name")
    rows = await read_rows(
        "SELECT name, org, ns_roles, public_keys, issuer, audience, claims, agent_claim, "
        "jwks_uri, max_access_token_s, platform_admin, active "
        "FROM loom.machine_identities FINAL WHERE name = {name:String} "
        "LIMIT 2 FORMAT JSONEachRow",
        {"name": name},
        deadline=deadline,
        row_bytes=64 * 1024,
        total_bytes=128 * 1024,
        max_rows=1,
        tag="cell.admin.machine",
        ch=ch or get_client(),
    )
    return rows[0] if rows else None


async def revocations(keys: list[str], *, deadline: float, ch: Any = None) -> list[dict[str, Any]]:
    """Read the complete finite token/subject keys, including owed revocations."""
    if (
        not keys
        or len(keys) != len(set(keys))
        or len(keys) > 16
        or any(not key or len(key.encode("utf-8")) > 1024 for key in keys)
    ):
        raise BoundedReadError("Invalid revocation key set")
    rows = await read_rows(
        "SELECT jti, outright, cutoff FROM ("
        "SELECT jti, isNull(issued_before) AS outright, "
        "toInt64(ifNull(toUnixTimestamp(issued_before), 0)) AS cutoff "
        "FROM loom.revoked_tokens FINAL "
        "WHERE jti IN {keys:Array(String)} AND expires_at > now() "
        "UNION ALL SELECT id AS jti, toUInt8(1) AS outright, toInt64(0) AS cutoff "
        "FROM loom.oauth_state FINAL WHERE record_type = 'revocation_owed' "
        "AND id IN {keys:Array(String)} AND expires_at > toUInt32(now())"
        ") LIMIT 65 FORMAT JSONEachRow",
        {"keys": keys},
        deadline=deadline,
        row_bytes=1024,
        total_bytes=64 * 1024,
        max_rows=64,
        tag="cell.admin.revocations",
        ch=ch or get_client(),
    )
    counts: Counter[str] = Counter()
    for row in rows:
        key = row.get("jti")
        if not isinstance(key, str) or key not in keys:
            raise BoundedReadError("Unexpected revocation key")
        counts[key] += 1
        if counts[key] > 4:
            raise BoundedReadError("Ambiguous revocation multiplicity")
    return rows
