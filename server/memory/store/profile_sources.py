"""The memories a user's profile is built from, and whether a profile is behind them.

Both profile builders read the same rows: the user's current facts, nothing
superseded, rejected, proposed, past its stated end or TTL, nothing about
someone else, no stored request to forget, and never a `high` row (readable by
key only, never folded into a profile). The free-text fold also keeps to
facts the extractor keyed on the user (`user_…`).

A structured profile carries the digest of the rows it was built from
(`sources_digest_sql`): the user's (memory_id, content hash) pairs, so an
edit, a deletion or a new fact changes it, and a recall or a reinforcement,
which change neither, does not.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client

# `{types:Array(String)}` is the caller's parameter.
CURRENT_FACTS = """
    tombstoned_at IS NULL
    -- a high row is readable by key only, never folded into the profile
    AND sensitivity != 'high'
    AND memory_type IN {types:Array(String)}
    AND status NOT IN ('superseded', 'rejected', 'proposed')  -- proposed: not a fact yet
    AND (valid_to IS NULL OR valid_to > now())
    AND (expires_at IS NULL OR expires_at > now())
    AND NOT arrayExists(t -> startsWith(t, 'attribute:'), tags)
    AND NOT has(tags, 'about:other')
    AND NOT has(tags, 'forget')
"""

# The memory types a structured profile reads.
STRUCTURED_TYPES = ["semantic", "preference", "decision"]

SCHEMA_TAG = "profile_schema:"
SOURCES_TAG = "profile_sources:"


def sources_digest_sql() -> str:
    """The aggregate that digests a user's source rows; 16 hex characters."""
    return (
        "substring(lower(hex(SHA256(arrayStringConcat(arraySort(groupArray("
        "concat(toString(memory_id), ':', content_sha256))), ',')))), 1, 16)"
    )


async def user_facts(
    *,
    org: str,
    namespace: str,
    user_id: str,
    types: list[str],
    limit: int,
    user_keys_only: bool = False,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The user's current facts, most confident and newest first, at most `limit`."""
    ch = ch or _default_client()
    # The fold reads facts whose subject is the user (the extractor keys them `user_…`).
    keys = "AND startsWith(memory_key, 'user_')" if user_keys_only else ""
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS memory_id, value,
               toString(coalesce(valid_at, created_at)) AS since
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND user_id = {{uid:String}} AND {CURRENT_FACTS}
          {keys}
        ORDER BY confidence DESC, created_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "uid": user_id,
            "types": types,
            "lim": int(limit),
        },
    )
    return rows


async def sources_digest(
    *, org: str, namespace: str, user_id: str, ch: _CHClient | None = None
) -> str:
    """The digest of the user's structured-profile sources now; '' when there are none."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT {sources_digest_sql()} AS digest, count() AS n
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND user_id = {{uid:String}} AND {CURRENT_FACTS}
        """,
        params={"org": org, "ns": namespace, "uid": user_id, "types": STRUCTURED_TYPES},
    )
    if not row or not int(row.get("n") or 0):
        return ""
    return str(row["digest"])


async def profile_row(
    *, org: str, namespace: str, user_id: str, label: str, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """The user's live structured profile row; None when none is built."""
    ch = ch or _default_client()
    row: dict[str, Any] | None = await ch.query_one(
        """
        SELECT toString(memory_id) AS memory_id, version, value, tags, updated_at
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND agent = '-'
          AND user_id = {uid:String} AND session_id = '-'
          AND memory_key = {label:String} AND memory_type = 'profile'
          AND tombstoned_at IS NULL
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "uid": user_id, "label": label},
    )
    return row


async def users_behind(
    *,
    org: str,
    namespace: str,
    label: str,
    schema_sha256: str,
    cooldown_seconds: int,
    limit: int,
    ch: _CHClient | None = None,
) -> list[str]:
    """Users of the namespace whose structured profile is missing, built under
    another schema, or behind their memories and older than the cooldown;
    those with none first, then the oldest, at most `limit`. A user whose
    memories are all gone is behind too: the rebuild empties the profile."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT if(src.user_id != '', src.user_id, p.user_id) AS uid
        FROM (
            SELECT user_id, {sources_digest_sql()} AS digest
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND user_id != '-' AND {CURRENT_FACTS}
            GROUP BY user_id
        ) AS src
        FULL JOIN (
            SELECT user_id, tags, updated_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND agent = '-'
              AND user_id != '-' AND session_id = '-' AND memory_key = {{label:String}}
              AND memory_type = 'profile' AND tombstoned_at IS NULL
        ) AS p ON p.user_id = src.user_id
        WHERE NOT has(p.tags, {{schema_tag:String}})
           OR (NOT has(p.tags, concat({{sources_tag:String}}, src.digest))
               AND p.updated_at <= now() - toIntervalSecond({{cool:UInt32}}))
        ORDER BY p.updated_at, uid
        LIMIT {{lim:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "types": STRUCTURED_TYPES,
            "label": label,
            "schema_tag": SCHEMA_TAG + schema_sha256[:16],
            "sources_tag": SOURCES_TAG,
            "cool": int(max(0, cooldown_seconds)),
            "lim": int(max(1, limit)),
        },
    )
    return [str(r["uid"]) for r in rows]
