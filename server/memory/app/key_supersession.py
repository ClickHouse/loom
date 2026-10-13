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
different items in different sessions. A bulk write (the path extraction writes
through) asks the conflict judge, before its insert, about the newest live rows of each
key and user it writes (`judge_same_key_rows`). A contradiction retires whichever row is
older, so an old session ingested late lands already superseded instead of burying the
current value; an elaboration by the newer row retires the older; any other label, or
none, keeps both."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from server.core.datetimes import parse_iso_dt, to_naive_utc
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.memory.app.conflict_judge import CONTRADICTION, ELABORATION

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


def _subject(row: Mapping[str, Any]) -> tuple[str, str]:
    """Whose attribute a row states: its user, or for the anonymous user `-` the
    agent that wrote it, so anonymous writers are not one subject."""
    user = str(row.get("user_id") or "-")
    return (user, "" if user != "-" else str(row.get("agent") or ""))


def same_key_peers(
    new_rows: list[dict[str, Any]], live_rows: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Pure: new row id -> the stored live rows of the same key and subject that
    the judge reads against it, newest first. Only a new row that is `active` or
    `conflicted` is judged (one the batch already retired or marked `duplicate`
    has a live twin that speaks for it); the write's own rows and pinned rows are
    never peers."""
    new_ids = {str(r.get("memory_id") or "") for r in new_rows}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in new_rows:
        rid = str(r.get("memory_id") or "")
        if str(r.get("status") or "active") not in ("active", "conflicted"):
            continue
        peers = [
            m
            for m in live_rows
            if m.get("memory_key") == r.get("memory_key")
            and _subject(m) == _subject(r)
            and str(m.get("memory_id") or "") not in new_ids
            and not bool(m.get("pinned"))
        ]
        if rid and peers:
            out[rid] = sorted(peers, key=_order, reverse=True)
    return out


@dataclass
class JudgedRetirements:
    """What the judge's labels retire. `new_rows`: (new row id, the peer it
    retires behind) for rows of this write that land superseded; `peers`: the
    stored rows that retire behind a row of this write (`_victim` dicts)."""

    new_rows: list[tuple[str, str]] = field(default_factory=list)
    peers: list[dict[str, Any]] = field(default_factory=list)


def judged_victims(
    new_rows: list[dict[str, Any]],
    peers_by_new: Mapping[str, list[dict[str, Any]]],
    labels: Mapping[tuple[str, str], str],
) -> JudgedRetirements:
    """Pure: which rows retire once the judge has labelled (new row id, peer id)
    pairs. Only the older row of a pair ever retires (`_order`), so two writers
    that judge each other cannot retire both. A contradiction from a newer peer
    retires the new row (behind the newest such peer) unless it is pinned, and a
    new row that retires retires nothing itself; otherwise a contradiction or an
    elaboration retires the older peer. Any other label, or none, retires
    nothing."""
    by_id = {str(r.get("memory_id") or ""): r for r in new_rows}
    out = JudgedRetirements()
    peer_victims: dict[str, dict[str, Any]] = {}
    for new_id, peers in peers_by_new.items():
        new = by_id[new_id]
        newer_contradiction = next(
            (
                p
                for p in peers
                if labels.get((new_id, str(p.get("memory_id") or ""))) == CONTRADICTION
                and _order(new) < _order(p)
            ),
            None,
        )
        if newer_contradiction is not None and not bool(new.get("pinned")):
            out.new_rows.append((new_id, str(newer_contradiction.get("memory_id") or "")))
            continue
        for peer in peers:
            peer_id = str(peer.get("memory_id") or "")
            if labels.get((new_id, peer_id)) in (CONTRADICTION, ELABORATION) and _order(
                peer
            ) < _order(new):
                peer_victims.setdefault(peer_id, _victim(peer, new_id))
    # A new row that retires behind a peer another new row retires points at that
    # row instead, so no `superseded_by` names a dead row.
    out.new_rows = [
        (n, str(peer_victims[w]["superseded_by"]) if w in peer_victims else w)
        for n, w in out.new_rows
    ]
    out.peers = list(peer_victims.values())
    return out


def _order(row: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    """Which of two rows is the later statement: when its fact became true
    (`valid_at`, else when it was written), then when it was written, then its id,
    so every pair has one older row. Times in UTC."""
    created = _utc(row.get("created_at"))
    return (
        _utc(row.get("valid_at")) or created or datetime.min,
        created or datetime.min,
        str(row.get("memory_id") or ""),
    )


def _utc(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return parse_iso_dt(to_naive_utc(raw))
    if isinstance(raw, str) and raw.strip():
        try:
            return parse_iso_dt(
                to_naive_utc(datetime.fromisoformat(raw.strip().replace("Z", "+00:00")))
            )
        except ValueError:
            return parse_iso_dt(raw)
    return None


class _Peer(NamedTuple):
    memory_id: str
    value: str


async def _judge_labels(
    new_rows: list[dict[str, Any]],
    peers_by_new: Mapping[str, list[dict[str, Any]]],
    *,
    concurrency: int,
) -> dict[tuple[str, str], str]:
    """One judge call per new row that has same-key peers, at most `concurrency`
    at once; a call that fails or is switched off labels nothing, and neither
    does a peer the model did not label."""
    from server.memory.app.conflicts import judge_peers

    by_id = {str(r.get("memory_id") or ""): r for r in new_rows}
    sem = asyncio.Semaphore(max(concurrency, 1))

    async def one(new_id: str, peers: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        async with sem:
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


def stamp_new_row_retirements(
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    retired: list[tuple[str, str]],
    peers: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> None:
    """Land each retired new row superseded: `status`, `superseded_by` and
    `invalid_at` (the winner's `valid_at`, else `now`) on its memory row and on its
    embedding row (matched by identity: embedding rows carry no id). Every memory
    row keeps the same key set, as `ch.insert` derives its columns from row 0."""
    for row in memory_rows:
        row.setdefault("superseded_by", "")
        row.setdefault("invalid_at", None)
    winner_of = dict(retired)
    ident = ("memory_key", "agent", "user_id", "session_id")
    emb_by_ident = {tuple(str(e.get(c) or "") for c in ident): e for e in embedding_rows}
    rows_by_id = {str(r.get("memory_id") or ""): r for r in memory_rows}
    for row in memory_rows:
        winner = winner_of.get(str(row.get("memory_id") or ""))
        if winner is None:
            continue
        w = peers.get(winner) or rows_by_id.get(winner) or {}
        inv = w.get("valid_at")
        inv = to_naive_utc(inv) if isinstance(inv, datetime) else now
        row["status"] = "superseded"
        row["superseded_by"] = winner
        row["invalid_at"] = inv
        e = emb_by_ident.get(tuple(str(row.get(c) or "") for c in ident))
        if e is not None:
            e["status"] = "superseded"
            e["invalid_at"] = inv


async def judge_same_key_rows(
    *,
    org: str,
    namespace: str,
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    ch: Any,
    now: datetime,
) -> JudgedRetirements:
    """Before a bulk insert under `auto_reinforce` with the judge on: read the
    newest live rows of each (key, subject) the write carries, judge them against
    the new rows and stamp the new rows that lose (`stamp_new_row_retirements`).
    The stored rows that lose are returned for `retire_judged_peers` after the
    insert. A 'high' row is neither judged nor a peer: its value never reaches the
    LLM. At most `bulk_conflict_check_max_entries` new rows are judged,
    `bulk_conflict_check_concurrency` calls at once, each against at most
    `memory_conflict_judge_max_peers` peers. Best effort: on any error nothing
    retires."""
    from server.settings import settings

    try:
        from server.memory.app.conflicts import (
            DEFAULT_CONFLICT_POLICY,
            judge_enabled,
            namespace_conflict_policy,
        )

        if not memory_rows or not judge_enabled():
            return JudgedRetirements()
        if await namespace_conflict_policy(org, namespace, ch=ch) != DEFAULT_CONFLICT_POLICY:
            return JudgedRetirements()
        cap = max(int(settings.bulk_conflict_check_max_entries), 0)
        judged_rows = [
            r
            for r in memory_rows
            if str(r.get("memory_key") or "")
            and str(r.get("status") or "active") in ("active", "conflicted")
            # A 'high' row's value never reaches an LLM (it keeps no vector either).
            and str(r.get("sensitivity") or "") != "high"
        ][:cap]
        if not judged_rows:
            return JudgedRetirements()
        params: dict[str, Any] = {
            "org": org,
            "ns": namespace,
            "keys": sorted({str(r["memory_key"]) for r in judged_rows}),
            "users": sorted({str(r.get("user_id") or "-") for r in judged_rows}),
            "ids": sorted({str(r.get("memory_id") or "") for r in memory_rows}),
            "n": max(int(settings.memory_conflict_judge_max_peers), 1),
        }
        # A write judges and retires only rows inside the writer's read scope.
        in_scope = scope_and(request_scope(), params)
        live = await ch.query_all(
            f"""
            SELECT toString(memory_id) AS memory_id, namespace, agent, user_id, session_id,
                   memory_key, value, pinned, valid_at, created_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_key IN {{keys:Array(String)}} AND user_id IN {{users:Array(String)}}
              AND tombstoned_at IS NULL AND pinned = 0 AND sensitivity != 'high'
              AND status IN ('active', 'duplicate', 'conflicted')
              AND toString(memory_id) NOT IN {{ids:Array(String)}}{in_scope}
            ORDER BY coalesce(valid_at, created_at) DESC, created_at DESC
            LIMIT {{n:UInt32}} BY memory_key, user_id, if(user_id = '-', agent, '')
            SETTINGS select_sequential_consistency = 1
            """,
            params=params,
        )
        peers = same_key_peers(judged_rows, live)
        if not peers:
            return JudgedRetirements()
        labels = await _judge_labels(
            judged_rows, peers, concurrency=int(settings.bulk_conflict_check_concurrency)
        )
        out = judged_victims(judged_rows, peers, labels)
        if out.new_rows:
            peer_by_id = {str(p.get("memory_id") or ""): p for ps in peers.values() for p in ps}
            stamp_new_row_retirements(memory_rows, embedding_rows, out.new_rows, peer_by_id, now)
        if out.new_rows or out.peers:
            log.info(
                "same-key judge: %d new and %d stored rows retire in %s/%s",
                len(out.new_rows),
                len(out.peers),
                org,
                namespace,
            )
        return out
    except Exception as e:  # noqa: BLE001 — the rows land as they are; retirement is best effort
        log.warning("same-key judge failed (non-fatal): %s", e)
        return JudgedRetirements()


async def retire_judged_peers(org: str, victims: list[dict[str, Any]], *, ch: Any) -> int:
    """After the insert: retire the stored rows `judge_same_key_rows` found older
    than a row of this write (a lock refuses it). Best effort. Returns how many."""
    if not victims:
        return 0
    try:
        from server.memory.domain.lock import refuses_retirement
        from server.memory.store import supersede_duplicates

        return await supersede_duplicates(org, victims, ch=ch, skip=refuses_retirement)
    except Exception as e:  # noqa: BLE001 — the rows are in; retirement is best effort
        log.warning("same-key judged retirement failed (non-fatal): %s", e)
        return 0


async def supersede_same_key_rows(
    *,
    org: str,
    namespace: str,
    new_rows: list[dict[str, Any]],
    ch: Any,
    policy: str | None = None,
) -> int:
    """Retire every older live row that shares a key with one of `new_rows`.

    Only under `newest_wins`; every other policy leaves same-key rows live (under
    `auto_reinforce` a bulk write judges them first, `judge_same_key_rows`).
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
