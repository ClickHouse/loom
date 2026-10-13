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
when).

Under the default `auto_reinforce` a shared key is not proof of one attribute:
extraction reuses generic keys (`assistant_recommended_3`, `user_attended`) for
different items in different sessions. A bulk write (`judged=True`, the path extraction
writes through) asks the conflict judge about each same-key row of the same user that
another session wrote. A contradiction retires whichever row is older in valid time, so
an old session ingested late does not bury the current value; an elaboration retires
the row it elaborates; any other label keeps both."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, NamedTuple

from server.core.datetimes import parse_iso_dt
from server.memory.app.conflict_judge import CONTRADICTION, ELABORATION

from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and

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


def same_key_peers(
    new_rows: list[dict[str, Any]], live_rows: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Pure: new row id -> the live rows of the same key and user that the judge
    reads against it, newest in valid time first. The write's own rows and pinned
    rows are never peers."""
    new_ids = {str(r.get("memory_id") or "") for r in new_rows}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in new_rows:
        rid = str(r.get("memory_id") or "")
        peers = [
            m
            for m in live_rows
            if m.get("memory_key") == r.get("memory_key")
            and str(m.get("user_id") or "") == str(r.get("user_id") or "")
            and str(m.get("memory_id") or "") not in new_ids
            and not bool(m.get("pinned"))
        ]
        if rid and peers:
            out[rid] = sorted(peers, key=_world_time, reverse=True)
    return out


def judged_victims(
    new_rows: list[dict[str, Any]],
    peers_by_new: Mapping[str, list[dict[str, Any]]],
    labels: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """Pure: which rows retire once the judge has labelled each (new row id, peer id)
    pair. A contradiction retires the pair's row that is older in valid time (the
    peer on a tie), an elaboration the peer; any other label retires nothing."""
    by_id = {str(r.get("memory_id") or ""): r for r in new_rows}
    out: dict[str, dict[str, Any]] = {}
    for new_id, peers in peers_by_new.items():
        new = by_id[new_id]
        for peer in peers:
            peer_id = str(peer.get("memory_id") or "")
            label = labels.get((new_id, peer_id))
            if label == CONTRADICTION and _world_time(new) < _world_time(peer):
                if not bool(new.get("pinned")):
                    out.setdefault(new_id, _victim(new, peer_id))
            elif label in (CONTRADICTION, ELABORATION):
                out.setdefault(peer_id, _victim(peer, new_id))
    return list(out.values())


def _world_time(row: Mapping[str, Any]) -> datetime:
    """When the row's fact became true: `valid_at`, else `created_at`."""
    return parse_iso_dt(row.get("valid_at")) or parse_iso_dt(row.get("created_at")) or datetime.min


class _Peer(NamedTuple):
    memory_id: str
    value: str


async def _judge_labels(
    new_rows: list[dict[str, Any]], peers_by_new: Mapping[str, list[dict[str, Any]]]
) -> dict[tuple[str, str], str]:
    """One judge call per new row that has same-key peers, all at once; a call
    that fails or is switched off labels nothing, and neither does a peer the
    model did not label."""
    from server.memory.app.conflicts import judge_peers

    by_id = {str(r.get("memory_id") or ""): r for r in new_rows}

    async def one(new_id: str, peers: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        judged = await judge_peers(
            str(by_id[new_id].get("value") or ""),
            [_Peer(str(p.get("memory_id") or ""), str(p.get("value") or "")) for p in peers],
        )
        if judged is None or not judged.ran:
            return {}
        # Only what the model said: a peer it left unlabelled is a contradiction to
        # the flag path, and must not be one to a retirement.
        return {(new_id, pid): label for pid, label in judged.stated.items()}

    labels: dict[tuple[str, str], str] = {}
    for part in await asyncio.gather(*(one(n, p) for n, p in peers_by_new.items())):
        labels |= part
    return labels


async def supersede_same_key_rows(
    *,
    org: str,
    namespace: str,
    new_rows: list[dict[str, Any]],
    ch: Any,
    policy: str | None = None,
    judged: bool = False,
) -> int:
    """Retire every older live row that shares a key with one of `new_rows`.

    Under `newest_wins`, every one. With `judged` under `auto_reinforce`, the ones
    the conflict judge reads as the same attribute (`judged_victims`); `new_rows`
    then carry `value`, `user_id`, `pinned` and `valid_at` too, and a new row older
    in valid time than its peer can be the one that retires. Every other policy
    leaves same-key rows live. Best effort, never fails the write. Returns how many
    retired."""
    if not new_rows:
        return 0
    try:
        from server.memory.app.conflicts import DEFAULT_CONFLICT_POLICY, judge_enabled

        if policy is None:
            from server.memory.app.conflicts import namespace_conflict_policy

            policy = await namespace_conflict_policy(org, namespace, ch=ch)
        judging = judged and policy == DEFAULT_CONFLICT_POLICY and judge_enabled()
        if policy != "newest_wins" and not judging:
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
                   memory_key, value, pinned, valid_at, created_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_key IN {{keys:Array(String)}} AND tombstoned_at IS NULL
              AND status IN ('active', 'duplicate', 'conflicted'){in_scope}
            SETTINGS select_sequential_consistency = 1
            """,
            params=params,
        )
        if judging:
            peers = same_key_peers(new_rows, live)
            victims = judged_victims(new_rows, peers, await _judge_labels(new_rows, peers))
        else:
            victims = same_key_victims(new_rows, live)
        if not victims:
            return 0
        from server.memory.domain.lock import refuses_retirement
        from server.memory.store import supersede_duplicates

        n = await supersede_duplicates(org, victims, ch=ch, skip=refuses_retirement)
        log.info(
            "same-key supersession (%s): %d rows retired behind %d keys in %s/%s",
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
