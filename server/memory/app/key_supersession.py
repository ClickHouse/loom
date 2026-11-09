"""Same-key supersession: when two rows of one key state one attribute, the newer
statement retires the older.

The storage identity includes agent, user and session, so a key written again from
another session is a second live row, and the conflict check skips same-key peers.
Under `newest_wins` the newest row of a key retires every older one
(`supersede_same_key_rows`). Under any other policy a shared key is not proof of one
attribute (extraction reuses generic keys): the conflict resolver judges the rows
(`memory.workers.conflict_resolver`); docs/architecture/memory-architecture.md has the
rules."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import server.memory.store as memory_repo
from server.core import pg
from server.core.naming import is_anonymous_user, session_spellings
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.memory.app import conflicts
from server.memory.app.auto_resolution import (
    SAME_KEY_NEWEST_WINS,
    Follow,
    RetirementTracker,
    owe_retirements,
    settle_retirements,
)
from server.memory.domain import lock
from server.memory.store import pg_memory

log = logging.getLogger(__name__)

# Suffixes an extractor adds to avoid "overwriting" an attribute it has seen
# (`andy_warhol_death_location_alternate`). The store keeps every version, so
# the key stays the attribute's. `old` / `previous` / `former` are NOT here:
# they name a different, closed-window fact.
VARIANT_SUFFIXES: frozenset[str] = frozenset(
    {
        "alternate",
        "alt",
        "new",
        "updated",
        "update",
        "revised",
        "corrected",
        "latest",
        "current",
        "changed",
        "change",
        "v2",
        "correction",
        # The extractor's verdict on a statement it disbelieves
        # (`france_capital_misstatement` beside `france_capital`) is not part of
        # the attribute; the update has to land on the attribute's key.
        "misstatement",
        "claim",
        "claimed",
        "error",
        "incorrect",
        "false",
        "mistake",
        "contradiction",
        "alleged",
        "belief",
    }
)


def strip_variant_suffix(key: str) -> str:
    """`andy_warhol_death_location_alternate` → `andy_warhol_death_location`.
    A key with fewer than three parts is left alone (`user_current` is a
    whole key, not a variant of `user`)."""
    parts = key.split("_")
    if len(parts) >= 3 and parts[-1] in VARIANT_SUFFIXES:
        return "_".join(parts[:-1])
    return key


def same_key_victims(
    new_rows: list[dict[str, Any]], live_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Pure: which live rows retire behind the rows a write just inserted.

    `new_rows` carry `memory_id` and `memory_key` (the last row for a key is
    the newest when a caller sent a key twice); `live_rows` are the
    namespace's live rows for those keys with their embedding identity
    (`namespace`, `agent`, `user_id`, `session_id`) and `pinned`. A live row
    retires when it shares a key with a new row, is not one of the new rows
    and is not pinned; `superseded_by` is the new row's id."""
    newest_by_key: dict[str, str] = {}
    new_ids: set[str] = set()
    for r in new_rows:
        key = str(r.get("memory_key") or "")
        mid = str(r.get("memory_id") or "")
        if not key or not mid:
            continue
        newest_by_key[key] = mid
        new_ids.add(mid)
    out: list[dict[str, Any]] = []
    for m in live_rows:
        key = str(m.get("memory_key") or "")
        mid = str(m.get("memory_id") or "")
        if key not in newest_by_key or not mid or mid in new_ids or bool(m.get("pinned")):
            continue
        out.append(_victim(m, newest_by_key[key]))
    return out


def _victim(row: Mapping[str, Any], superseded_by: str) -> dict[str, Any]:
    return {
        "memory_id": str(row.get("memory_id") or ""),
        "namespace": str(row.get("namespace") or ""),
        "agent": str(row.get("agent") or ""),
        "user_id": str(row.get("user_id") or ""),
        "session_id": str(row.get("session_id") or ""),
        "memory_key": str(row.get("memory_key") or ""),
        "value": str(row.get("value") or ""),
        "superseded_by": superseded_by,
    }


async def keys_held_elsewhere(
    *, org: str, namespace: str, memory_rows: list[dict[str, Any]], ch: Any
) -> set[str]:
    """The keys among a write's rows (one writer identity) that hold a live row of the
    same user from another session. Outside `newest_wins` the conflict resolver judges
    such a pair, since a shared key is not proof of one attribute. None for the
    anonymous user (`-`, or a user_id sent empty), whose sessions are separate tasks: a
    verdict there could only clear a flag, and a fresh write has none. Inside the
    writer's read scope only; a 'high' row is never a peer."""
    if not memory_rows:
        return set()
    # The stored values as they are: a session sent empty is stored "", so a '-'
    # fallback would count the writer's own row as another session's.
    writer = memory_rows[0]
    user = str(writer["user_id"])
    if is_anonymous_user(user):
        return set()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "keys": sorted({str(r.get("memory_key") or "") for r in memory_rows} - {""}),
        "own_agent": str(writer["agent"]),
        "own_user": user,
        "own_sessions": session_spellings(str(writer["session_id"])),
    }
    if pg.enabled():
        # The authority's heads: a first write of the key elsewhere answers at its
        # commit, before ClickHouse holds it.
        own = set(params["own_sessions"])
        heads = await pg_memory.live_key_heads(
            org, namespace, params["keys"], scope=request_scope()
        )
        return {
            str(h["memory_key"])
            for h in heads
            if str(h["user_id"]) == user
            and not (str(h["agent"]) == params["own_agent"] and str(h["session_id"]) in own)
            and h.get("sensitivity") != "high"
            and h.get("status") in ("active", "conflicted")
        }
    in_scope = scope_and(request_scope(), params)
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT memory_key
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key IN {{keys:Array(String)}} AND user_id = {{own_user:String}}
          AND NOT (agent = {{own_agent:String}}
                   AND session_id IN {{own_sessions:Array(String)}})
          AND tombstoned_at IS NULL AND sensitivity != 'high'
          AND status IN ('active', 'conflicted'){in_scope}
        """,
        params=params,
    )
    return {str(r["memory_key"]) for r in rows}


_SUPERSEDABLE = ("active", "duplicate", "conflicted")


async def _live_same_key_rows(
    org: str, namespace: str, keys: list[str], *, ch: Any
) -> list[dict[str, Any]]:
    """The live rows of `keys` in the writer's read scope that a same-key write may retire,
    each with its identity, value and `pinned`. Under Postgres the authority's heads: a
    first write of the key in another session answers at its commit, before ClickHouse
    holds it."""
    if pg.enabled():
        heads = await pg_memory.live_key_heads(org, namespace, keys, scope=request_scope())
        return [
            {
                "memory_id": str(h["memory_id"]),
                **{c: h[c] for c in ("namespace", "agent", "user_id", "session_id")},
                "memory_key": h["memory_key"],
                "value": h.get("value"),
                "pinned": h.get("pinned"),
            }
            for h in heads
            if h.get("status") in _SUPERSEDABLE
        ]
    # The rows this write inserted moments ago must be visible so they are recognised as
    # the new rows and not retired themselves (CH Cloud replicas; no-op on plain
    # MergeTree).
    params: dict[str, Any] = {"org": org, "ns": namespace, "keys": keys}
    # A write retires only rows inside the writer's read scope.
    in_scope = scope_and(request_scope(), params)
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS memory_id, namespace, agent, user_id, session_id,
               memory_key, value, pinned
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key IN {{keys:Array(String)}} AND tombstoned_at IS NULL
          AND status IN ('active', 'duplicate', 'conflicted'){in_scope}
        SETTINGS select_sequential_consistency = 1
        """,
        params=params,
    )
    return rows


async def supersede_same_key_rows(
    *,
    org: str,
    namespace: str,
    new_rows: list[dict[str, Any]],
    ch: Any,
    follows: list[Follow],
    policy: str | None = None,
    agent: str = "",
) -> int:
    """Retire every older live row that shares a key with one of `new_rows`; the
    retired pairs go to `follows`, for the caller to follow once its relations are in.

    Only under `newest_wins`; every other policy leaves same-key rows live for the
    conflict resolver to judge.
    Best effort, never fails the write. Returns how many retired."""
    if not new_rows:
        return 0
    try:
        if policy is None:
            policy = await conflicts.namespace_conflict_policy(org, namespace, ch=ch)
        if policy != "newest_wins":
            return 0
        keys = sorted({str(r.get("memory_key") or "") for r in new_rows} - {""})
        if not keys:
            return 0
        live = await _live_same_key_rows(org, namespace, keys, ch=ch)
        victims = same_key_victims(new_rows, live)
        if not victims:
            return 0
        # The tracker also refuses a row retired since the read (it is not re-pointed).
        tracker = RetirementTracker(lock.refuses_retirement)
        planned = [{**v, "superseded_by_key": v["memory_key"]} for v in victims]
        intent = await owe_retirements(
            org=org, namespace=namespace, victims=planned, reason=SAME_KEY_NEWEST_WINS, agent=agent
        )
        try:
            # Read-your-writes: a lagging replica's row would be rebuilt over a revision.
            n = await memory_repo.supersede_duplicates(
                org,
                victims,
                namespace=namespace,
                ch=ch,
                skip=tracker.skip,
                strong=True,
                on_written=tracker.written,
                on_unconfirmed=tracker.unconfirmed,
            )
        finally:
            # What reached loom.memory is recorded even when the embeddings mirror failed.
            await settle_retirements(
                org=org,
                namespace=namespace,
                retired=[
                    {**v, "superseded_by_key": v["memory_key"]} for v in tracker.retired(victims)
                ],
                reason=SAME_KEY_NEWEST_WINS,
                follows=follows,
                agent=agent,
                intent=intent,
            )
        log.info(
            "same-key supersession (%s): %d older rows retired behind %d keys in %s/%s",
            policy,
            n,
            len({v["memory_key"] for v in victims}),
            org,
            namespace,
        )
        return n
    except Exception as e:  # noqa: BLE001 — the rows are in; retirement is best effort
        log.warning("same-key supersession failed (non-fatal): %s", e)
        return 0
