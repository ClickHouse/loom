"""A memory key names one attribute of one subject in a
namespace, and under the `newest_wins` conflict policy the newest row for a
key is the live one, whatever agent, user or session wrote it.

The write paths carry the same key from different sessions as different rows (the
ReplacingMergeTree identity includes agent, user and session), and the semantic conflict
check excludes the row's own key on the assumption that a re-write is last-writer-wins
by version. Across sessions it is not: the same key can hold two live rows, the pre-edit
value and the post-edit value, both `active`. Here the newer row retires the older ones
through the same version re-emit dedup uses, so recall drops them and the audit trail
keeps them (`superseded_by` points at the row that replaced them, `invalid_at` says
when)."""

from __future__ import annotations

import logging
from typing import Any

from server.identity.domain.read_scope import scope_and
from server.memory.app.provenance import request_scope

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
        out.append(
            {
                "memory_id": mid,
                "namespace": str(m.get("namespace") or ""),
                "agent": str(m.get("agent") or ""),
                "user_id": str(m.get("user_id") or ""),
                "session_id": str(m.get("session_id") or ""),
                "memory_key": key,
                "value": str(m.get("value") or ""),
                "superseded_by": newest_by_key[key],
            }
        )
    return out


async def supersede_same_key_rows(
    *,
    org: str,
    namespace: str,
    new_rows: list[dict[str, Any]],
    ch: Any,
    policy: str | None = None,
) -> int:
    """Retire every older live row that shares a key with one of `new_rows`.

    Only under `newest_wins`; every other policy leaves same-key rows live.
    Best effort, never fails the write. Returns how many retired."""
    if not new_rows:
        return 0
    try:
        if policy is None:
            from server.memory.app.conflicts import namespace_conflict_policy

            policy = await namespace_conflict_policy(org, namespace, ch=ch)
        if policy != "newest_wins":
            return 0
        keys = sorted({str(r.get("memory_key") or "") for r in new_rows} - {""})
        if not keys:
            return 0
        # The rows this write inserted moments ago must be visible so they are
        # recognised as the new rows and not retired themselves (CH Cloud
        # replicas; no-op on plain MergeTree).
        params: dict[str, Any] = {"org": org, "ns": namespace, "keys": keys}
        # A write retires only rows inside the writer's read scope.
        in_scope = scope_and(request_scope(), params)
        live = await ch.query_all(
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
        victims = same_key_victims(new_rows, live)
        if not victims:
            return 0
        from server.memory.domain.lock import refuses_retirement
        from server.memory.store import supersede_duplicates

        n = await supersede_duplicates(org, victims, ch=ch, skip=refuses_retirement)
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
