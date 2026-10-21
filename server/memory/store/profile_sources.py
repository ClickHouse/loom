"""The memories a user's profile is built from, and whether a profile is behind them.

Both profile builders read the same rows: the user's current facts, nothing
superseded, rejected, proposed, a duplicate, past its stated end or TTL, nothing about
someone else, no stored request to forget, and never a `high` row (readable by
key only, never folded into a profile). The free-text fold also keeps to
facts the extractor keyed on the user (`user_…`).

A structured profile carries its tags (`profile_tags`): the hash of the schema
it was built under, the digest of the rows it was built from (`SOURCES_DIGEST`,
over each row's id, content hash, confidence and date) and their count. An
edit, a revert, a restore, a deletion, a new fact or a restatement changes the
digest; a recall or a backfill, which keep the row's `updated_at`, does not.
Its `valid_at` is when its sources were read.

Every reader takes the request's read scope (`server.identity.domain.read_scope`):
a worker passes `WHOLE_NAMESPACE`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.core.ch import statement_timeout
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import ReadFilters, scope_and

# `{types:Array(String)}` is the caller's parameter.
CURRENT_FACTS = """
    tombstoned_at IS NULL
    -- a high row is readable by key only, never folded into the profile
    AND sensitivity != 'high'
    AND memory_type IN {types:Array(String)}
    -- proposed: not a fact yet; duplicate: its canonical row is the fact
    AND status NOT IN ('superseded', 'rejected', 'proposed', 'duplicate')
    AND (valid_to IS NULL OR valid_to > now())
    AND (expires_at IS NULL OR expires_at > now())
    AND NOT arrayExists(t -> startsWith(t, 'attribute:'), tags)
    AND NOT has(tags, 'about:other')
    AND NOT has(tags, 'forget')
"""

# The memory types a structured profile reads.
STRUCTURED_TYPES = ["semantic", "preference", "decision"]

# The aggregate that digests a user's source rows: an XOR of each row's hash over
# what a build reads and ranks by (the row, its content, its confidence, and the date
# the build reads and orders by, `valid_at` else `updated_at`), so it is fixed in size
# and the order of rows does not matter. A restatement writes a new `updated_at`, so it
# changes the digest and costs one rebuild after the cooldown; a recall or a backfill
# carries `updated_at` forward and changes nothing. `hex` drops leading zeros, so it
# is padded to 16 hex characters.
SOURCES_DIGEST = (
    "leftPad(lower(hex(groupBitXor(sipHash64(memory_id, content_sha256, confidence, "
    "coalesce(valid_at, updated_at))))), 16, '0')"
)

# A live structured profile row: not deleted, expired or redacted. A 'high' row is
# readable by key only, so profile.structured serves none and the worker rebuilds it.
_LIVE_PROFILE = """
    agent = '-' AND session_id = '-' AND memory_key = {label:String}
    AND memory_type = 'profile' AND tombstoned_at IS NULL AND redacted_at IS NULL
    AND sensitivity != 'high'
    AND (expires_at IS NULL OR expires_at > now())
"""


def schema_tag(schema_sha256: str) -> str:
    return "profile_schema:" + schema_sha256[:16]


def sources_tag(sources_digest: str) -> str:
    return "profile_sources:" + sources_digest


def profile_tags(schema_sha256: str, sources_digest: str, source_count: int) -> list[str]:
    """The tags a profile built under this schema from these sources carries; the
    count lets a deletion skip the rebuild cooldown."""
    return [
        schema_tag(schema_sha256),
        sources_tag(sources_digest),
        f"profile_count:{source_count}",
    ]


def is_current(tags: list[str], schema_sha256: str, sources_digest: str) -> bool:
    return {schema_tag(schema_sha256), sources_tag(sources_digest)} <= set(tags)


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
    """The user's current facts, most confident and most recently changed first, at
    most `limit`; each dated by when it became true, else when it last changed."""
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
               toString(coalesce(valid_at, updated_at)) AS since
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND user_id = {{uid:String}} AND {CURRENT_FACTS}
          {keys}{scope_and(scope, params)}
        ORDER BY confidence DESC, updated_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return rows


async def sources_digest(
    *,
    org: str,
    namespace: str,
    user_id: str,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> tuple[str, int]:
    """The user's sources digest and count now; ('', 0) when there are none."""
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "uid": user_id,
        "types": STRUCTURED_TYPES,
    }
    row = await ch.query_one(
        f"""
        SELECT {SOURCES_DIGEST} AS digest, count() AS n
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND user_id = {{uid:String}} AND {CURRENT_FACTS}{scope_and(scope, params)}
        """,
        params=params,
    )
    if not row or not int(row["n"]):
        return "", 0
    return str(row["digest"]), int(row["n"])


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


async def profile_head_hold(
    *,
    org: str,
    namespace: str,
    user_id: str,
    label: str,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """The pin and lock of the user's structured profile key's head, redacted or
    expired: a write to the key checks its head, not the row a reader is served."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "uid": user_id, "label": label}
    row: dict[str, Any] | None = await ch.query_one(
        f"""
        SELECT pinned, lock_mode, lock_until
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND user_id = {{uid:String}}
          AND agent = '-' AND session_id = '-' AND memory_key = {{label:String}}
          AND memory_type = 'profile' AND tombstoned_at IS NULL{scope_and(scope, params)}
        LIMIT 1
        """,
        params=params,
    )
    return row


@dataclass(frozen=True)
class Behind:
    """A user whose profile is behind, with what a refusal is keyed on."""

    user_id: str
    digest: str
    profile_version: int


async def users_behind(
    *,
    org: str,
    namespace: str,
    label: str,
    schema_sha256: str,
    cooldown_seconds: int,
    limit: int,
    skip: tuple[str, ...] = (),
    refused: dict[str, tuple[str, int]] | None = None,
    timeout_seconds: float,
    max_memory_bytes: int,
    scope: ReadFilters,
    ch: _CHClient | None = None,
) -> list[Behind]:
    """Users of the namespace whose structured profile is missing, built under
    another schema, or built from other sources than they have now: at most
    `limit`, none of `skip`, and none still at the (digest, profile version) a
    refusal in `refused` was made at. Those with no profile under this schema
    come first, then the oldest; refused users last. The statement runs under
    `timeout_seconds` and `max_memory_bytes`.

    One aggregate digests every user's current facts, so the answer is exact: a
    recall or a backfill changes no digest, and an edit, a revert, a restore, a
    restatement (a new `updated_at`), a deletion, an expiry or a passed `valid_to`
    does. A rebuild waits out the
    cooldown, except when the user has fewer sources than the profile was built
    from (a deletion), and a user whose sources are all gone is behind too."""
    ch = ch or _default_client()
    refused = refused or {}
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "types": STRUCTURED_TYPES,
        "skip": list(skip),
        "refused": sorted(refused),
        "label": label,
        "schema_tag": schema_tag(schema_sha256),
        "cool": cooldown_seconds,
        "lim": limit + len(refused),
    }
    # join_use_nulls = 0: a user missing from one side reads '' / 0 / [], so
    # `NOT has(p.tags, ...)` is true for a user with no profile, never NULL.
    # The scan reads a whole namespace's current facts: one that runs past these caps
    # fails (the worker counts it and backs the namespace off) instead of holding the pass.
    sql = f"""
        SELECT if(s.user_id != '', s.user_id, p.user_id) AS who, s.digest AS digest,
               p.version AS profile_version
        FROM (
            SELECT user_id, {SOURCES_DIGEST} AS digest, count() AS n
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND user_id != '-'
              AND {CURRENT_FACTS}{scope_and(scope, params)}
            GROUP BY user_id
        ) AS s
        FULL JOIN (
            SELECT user_id, tags, updated_at, version,
                   toUInt32OrZero(substring(
                       arrayFirst(t -> startsWith(t, 'profile_count:'), tags), 15
                   )) AS built_from
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}} AND user_id != '-'
              AND {_LIVE_PROFILE}{scope_and(scope, params)}
        ) AS p ON p.user_id = s.user_id
        WHERE if(s.user_id != '', s.user_id, p.user_id) NOT IN {{skip:Array(String)}}
          AND (NOT has(p.tags, {{schema_tag:String}})
               OR (NOT has(p.tags, concat('profile_sources:', s.digest))
                   AND (s.n < p.built_from
                        OR p.updated_at <= now() - toIntervalSecond({{cool:UInt32}}))))
        ORDER BY has({{refused:Array(String)}}, who), has(p.tags, {{schema_tag:String}}),
                 p.updated_at, who
        LIMIT {{lim:UInt32}}
        SETTINGS join_use_nulls = 0, max_memory_usage = {int(max_memory_bytes)}
    """
    with statement_timeout(timeout_seconds):
        rows = await ch.query_all(sql, params=params)
    behind = [Behind(r["who"], r["digest"], int(r["profile_version"])) for r in rows]
    # A refused user comes back once their digest or their profile row moved on
    # (an unpin, an unlock, an edit).
    behind = [b for b in behind if refused.get(b.user_id) != (b.digest, b.profile_version)]
    return behind[:limit]
