"""The memories a user's profile is built from, and whether a profile is behind them.

Both profile builders read the same rows: the user's current facts, nothing
superseded, rejected, proposed, past its stated end or TTL, nothing about
someone else, no stored request to forget, and never a `high` row (readable by
key only, never folded into a profile). The free-text fold also keeps to
facts the extractor keyed on the user (`user_…`).

A structured profile carries two tags (`profile_tags`): the hash of the schema
it was built under and the digest of the rows it was built from
(`SOURCES_DIGEST`), the user's (memory_id, content hash) pairs. An edit, a
deletion or a new fact changes the digest; a recall or a reinforcement, which
change neither, does not. Its `valid_at` is when its sources were read.

Every reader takes the request's read scope (`server.identity.domain.read_scope`):
a worker passes `WHOLE_NAMESPACE`.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters, scope_and

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

# The aggregate that digests a user's source rows; 16 hex characters.
SOURCES_DIGEST = (
    "substring(lower(hex(SHA256(arrayStringConcat(arraySort(groupArray("
    "concat(toString(memory_id), ':', content_sha256))), ',')))), 1, 16)"
)

# A live structured profile row: not deleted, expired or redacted.
_LIVE_PROFILE = """
    agent = '-' AND session_id = '-' AND memory_key = {label:String}
    AND memory_type = 'profile' AND tombstoned_at IS NULL AND redacted_at IS NULL
    AND (expires_at IS NULL OR expires_at > now())
"""


def schema_tag(schema_sha256: str) -> str:
    return "profile_schema:" + schema_sha256[:16]


def profile_tags(schema_sha256: str, sources_digest: str) -> list[str]:
    """The tags a profile built under this schema from these sources carries."""
    return [schema_tag(schema_sha256), "profile_sources:" + sources_digest]


def is_current(tags: list[str], schema_sha256: str, sources_digest: str) -> bool:
    return set(profile_tags(schema_sha256, sources_digest)) <= set(tags)


async def user_facts(
    *,
    org: str,
    namespace: str,
    user_id: str,
    types: list[str],
    limit: int,
    scope: ReadFilters,
    user_keys_only: bool = False,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The user's current facts, most confident and newest first, at most `limit`."""
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "uid": user_id,
        "types": types,
        "lim": int(limit),
    }
    # The fold reads facts whose subject is the user (the extractor keys them `user_…`).
    keys = "AND startsWith(memory_key, 'user_')" if user_keys_only else ""
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS memory_id, value,
               toString(coalesce(valid_at, created_at)) AS since
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND user_id = {{uid:String}} AND {CURRENT_FACTS}
          {keys}{scope_and(scope, params)}
        ORDER BY confidence DESC, created_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return rows


async def digests(
    *,
    org: str,
    namespace: str,
    user_ids: list[str],
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> dict[str, str]:
    """The digest of each of `user_ids` that has sources, in one statement."""
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "uids": user_ids,
        "types": STRUCTURED_TYPES,
    }
    rows = await ch.query_all(
        f"""
        SELECT user_id, {SOURCES_DIGEST} AS digest
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND user_id IN {{uids:Array(String)}} AND {CURRENT_FACTS}{scope_and(scope, params)}
        GROUP BY user_id
        """,
        params=params,
    )
    return {str(r["user_id"]): str(r["digest"]) for r in rows}


async def sources_digest(
    *,
    org: str,
    namespace: str,
    user_id: str,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> str:
    """The digest of the user's structured-profile sources now; '' when there are none."""
    found = await digests(org=org, namespace=namespace, user_ids=[user_id], scope=scope, ch=ch)
    return found.get(user_id, "")


async def profile_row(
    *,
    org: str,
    namespace: str,
    user_id: str,
    label: str,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """The user's live structured profile row; None when none is built, or it
    expired or was redacted."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "uid": user_id, "label": label}
    row: dict[str, Any] | None = await ch.query_one(
        f"""
        SELECT toString(memory_id) AS memory_id, version, value, tags, updated_at,
               pinned, lock_mode, lock_until
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND user_id = {{uid:String}}
          AND {_LIVE_PROFILE}{scope_and(scope, params)}
        LIMIT 1
        """,
        params=params,
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
    candidates: int,
    skip: tuple[str, ...] = (),
    refused: dict[str, str] | None = None,
    ch: _CHClient | None = None,
) -> list[str]:
    """Users of the namespace whose structured profile is missing, built under
    another schema, or behind their memories; at most `limit`, none of `skip`, and
    none whose digest is still the one `refused` holds for them. Those with no
    profile under this schema come first, then the most recently changed.

    Two statements. The first lists at most `candidates` users with a row that
    changed since their profile's sources were read. A row's change time is the
    first time each of its states was written (value, live or deleted, status,
    sensitivity, type, the tags that exclude a fact), read from
    `loom.memory_versions`, which keeps every version where `loom.memory`'s merges
    keep only the newest, so a restatement, a recall or a backfill never looks new;
    an expiry or a `valid_to` that passed counts as a change too. Older than the
    cooldown only, except a deletion, which rebuilds at once. The second digests
    only those candidates."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT a.user_id AS uid, p.tags AS tags
        FROM (
            SELECT user_id, max(changed) AS last, max(deleted) AS last_deleted
            FROM (
                SELECT user_id,
                       greatest(
                           min(updated_at),
                           coalesce(maxIf(expires_at, expires_at <= now()), toDateTime(0)),
                           coalesce(maxIf(valid_to, valid_to <= now()), toDateTime(0))
                       ) AS changed,
                       if(live, toDateTime(0), min(updated_at)) AS deleted
                FROM loom.memory_versions
                WHERE org = {{org:String}} AND namespace = {{ns:String}} AND user_id != '-'
                  AND memory_key != {{label:String}} AND user_id NOT IN {{skip:Array(String)}}
                GROUP BY user_id, memory_id, content_sha256, isNull(tombstoned_at) AS live,
                         status, sensitivity, memory_type,
                         arrayExists(
                             t -> startsWith(t, 'attribute:') OR t IN ('about:other', 'forget'),
                             tags
                         )
            )
            GROUP BY user_id
        ) AS a
        LEFT JOIN (
            SELECT user_id, tags, updated_at, coalesce(valid_at, updated_at) AS read_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND user_id != '-'
              AND {_LIVE_PROFILE}
        ) AS p ON p.user_id = a.user_id
        WHERE NOT has(p.tags, {{schema_tag:String}})
           OR a.last_deleted >= p.read_at
           OR (a.last >= p.read_at AND p.updated_at <= now() - toIntervalSecond({{cool:UInt32}}))
        ORDER BY has(p.tags, {{schema_tag:String}}), a.last DESC, uid
        LIMIT {{cand:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "skip": list(skip),
            "label": label,
            "schema_tag": schema_tag(schema_sha256),
            "cool": int(max(0, cooldown_seconds)),
            "cand": int(max(1, candidates)),
        },
    )
    if not rows:
        return []
    uids = [str(r["uid"]) for r in rows]
    now = await digests(org=org, namespace=namespace, user_ids=uids, scope=WHOLE_NAMESPACE, ch=ch)
    refused = refused or {}
    behind = [
        str(r["uid"])
        for r in rows
        if not is_current(list(r["tags"] or []), schema_sha256, now.get(str(r["uid"]), ""))
        and refused.get(str(r["uid"])) != now.get(str(r["uid"]), "")
    ]
    return behind[: max(1, limit)]
