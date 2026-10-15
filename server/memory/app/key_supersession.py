"""Same-key supersession: when two rows of one key state one attribute, the newer
statement retires the older.

The storage identity includes agent, user and session, so a key written again from
another session is a second live row, and the conflict check skips same-key peers.
Under `newest_wins` the newest row of a key retires every older one
(`supersede_same_key_rows`). Under `auto_reinforce` a shared key is not proof of one
attribute (extraction reuses generic keys), so a bulk write asks the conflict judge
before its insert (`judge_same_key_rows`); docs/architecture/memory-architecture.md
has the rules."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, NamedTuple

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now, parse_iso_dt, to_naive_utc
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.memory.app import conflicts
from server.memory.app.auto_resolution import (
    SAME_KEY_NEWEST_WINS,
    RetirementTracker,
    schedule_auto_supersessions,
)
from server.memory.app.conflict_judge import CONTRADICTION, ELABORATION
from server.memory.domain import lock
from server.memory.domain.conflicts import preserves_quantities
from server.settings import settings

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
    """Pure: new row id -> the stored rows of its key, newest first. The peer read
    has already kept only the writer's subject, live and unpinned rows."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for m in live_rows:
        by_key.setdefault(str(m.get("memory_key") or ""), []).append(m)
    return {
        str(r["memory_id"]): sorted(peers, key=_rank, reverse=True)
        for r in new_rows
        if (peers := by_key.get(str(r.get("memory_key") or "")))
    }


@dataclass
class JudgedRetirements:
    """What the judge's labels retire. `new_rows`: (new row id, the peer it
    retires behind) for rows of this write that land superseded; `peers`: the
    stored rows that retire behind a row of this write (`_victim` dicts, each with
    the judge's `label`); `new_row_labels`: new row id -> the label that retired it."""

    new_rows: list[tuple[str, str]] = field(default_factory=list)
    peers: list[dict[str, Any]] = field(default_factory=list)
    new_row_labels: dict[str, str] = field(default_factory=dict)
    # The version of each stored row a new row lands behind, as the judge read it.
    winner_versions: dict[str, Any] = field(default_factory=dict)


def judged_victims(
    new_rows: list[dict[str, Any]],
    peers_by_new: Mapping[str, list[dict[str, Any]]],
    labels: Mapping[tuple[str, str], str],
) -> JudgedRetirements:
    """Pure: the rows the judge's labels retire. Only the older row of a pair
    retires (`_rank`), and only if the newer keeps its amounts. A contradiction from
    a newer peer lands the new row superseded (unless `_may_retire` refuses), and that
    row then retires nothing; otherwise a contradiction or elaboration retires the
    older peer. See docs/architecture/memory-architecture.md."""

    def retires(newer: Mapping[str, Any], older: Mapping[str, Any]) -> bool:
        return _older(older, newer) and preserves_quantities(
            str(newer.get("value") or ""), str(older.get("value") or "")
        )

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
                and retires(p, new)
            ),
            None,
        )
        if newer_contradiction is not None and _may_retire(new):
            out.new_rows.append((new_id, str(newer_contradiction.get("memory_id") or "")))
            out.new_row_labels[new_id] = CONTRADICTION
            out.winner_versions[str(newer_contradiction.get("memory_id") or "")] = (
                newer_contradiction.get("version")
            )
            continue
        for peer in peers:
            peer_id = str(peer.get("memory_id") or "")
            label = labels.get((new_id, peer_id))
            if label in (CONTRADICTION, ELABORATION) and retires(new, peer):
                # The version judged: a peer revised since is not the row the judge read.
                victim = {
                    **_victim(peer, new_id),
                    "judged_version": peer.get("version"),
                    "label": label,
                }
                peer_victims.setdefault(peer_id, victim)
    out.peers = list(peer_victims.values())
    return out


def _may_retire(row: Mapping[str, Any]) -> bool:
    """A new row may land superseded unless it is pinned or carries a lock in force
    (a revision of a locked key keeps the lock; `no_delete` lets the value change
    but refuses the retirement)."""
    return not bool(row.get("pinned")) and not lock.refuses_retirement(
        SimpleNamespace(lock_mode=row.get("lock_mode"), lock_until=row.get("lock_until"))
    )


def _rank(row: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    """When a row's statement holds, as one total order: its valid time, else when
    it was written (`updated_at`: a revision carries `created_at` forward from the
    key's first version), capped at when it was written (a fact dated in the future
    is still stated when it was written); then when it was written; then its id, an
    arbitrary but fixed tie-break. The SQL that picks the newest peers orders by the
    same key. Times in UTC, to the second."""
    written = _utc(row.get("updated_at")) or _utc(row.get("created_at")) or datetime.min
    valid = _utc(row.get("valid_at")) or written
    return (min(valid, written), written, str(row.get("memory_id") or ""))


def _older(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether `a` is the earlier statement of the two (`_rank`)."""
    return _rank(a) < _rank(b)


def _utc(raw: Any) -> datetime | None:
    """A datetime or ISO string as naive UTC to the second, as ClickHouse stores it;
    None for anything else or outside the `DateTime` range."""
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(raw, datetime):
        return None
    dt = parse_iso_dt(to_naive_utc(raw))
    return dt.replace(microsecond=0) if dt is not None else None


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
    by_id = {str(r.get("memory_id") or ""): r for r in new_rows}
    sem = asyncio.Semaphore(max(concurrency, 1))

    async def one(new_id: str, peers: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        async with sem:
            judged = await conflicts.judge_peers(
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
    now: datetime,
) -> None:
    """Land each retired new row superseded: `status`, `superseded_by` and
    `invalid_at` = `now` (when the record retired it, as `supersede_duplicates`
    stamps a stored row) on its memory row and on its embedding row, matched by
    identity and version (embedding rows carry no id)."""
    winner_of = dict(retired)
    ident = ("memory_key", "agent", "user_id", "session_id", "version")
    emb_by_ident = {tuple(str(e.get(c) or "") for c in ident): e for e in embedding_rows}
    for row in memory_rows:
        winner = winner_of.get(str(row.get("memory_id") or ""))
        if winner is None:
            continue
        row["status"] = "superseded"
        row["superseded_by"] = winner
        row["invalid_at"] = now
        e = emb_by_ident.get(tuple(str(row.get(c) or "") for c in ident))
        if e is not None:
            e["status"] = "superseded"
            e["invalid_at"] = now


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
    try:
        if not memory_rows or not conflicts.judge_enabled():
            return JudgedRetirements()
        if (
            await conflicts.namespace_conflict_policy(org, namespace, ch=ch)
            != conflicts.DEFAULT_CONFLICT_POLICY
        ):
            return JudgedRetirements()
        cap = max(int(settings.bulk_conflict_check_max_entries), 0)
        # Entries of a bulk share one identity, so two entries of one key are two
        # versions of one row: only the highest (the one FINAL keeps) is judged.
        winner: dict[str, dict[str, Any]] = {}
        for r in memory_rows:
            key = str(r.get("memory_key") or "")
            if key not in winner or int(r.get("version") or 0) >= int(
                winner[key].get("version") or 0
            ):
                winner[key] = r
        # A row the batch already retired or marked `duplicate` has a live twin that
        # speaks for it; a 'high' row's value never reaches an LLM.
        judged_rows = [
            r
            for r in winner.values()
            if r.get("memory_key")
            and str(r.get("status") or "active") in ("active", "conflicted")
            and str(r.get("sensitivity") or "") != "high"
        ][:cap]
        if not judged_rows:
            return JudgedRetirements()
        writer = memory_rows[0]  # a bulk writes under one identity
        params: dict[str, Any] = {
            "org": org,
            "ns": namespace,
            "keys": sorted({str(r["memory_key"]) for r in judged_rows}),
            "ids": sorted({str(r.get("memory_id") or "") for r in memory_rows}),
            "n": max(int(settings.memory_conflict_judge_max_peers), 1),
            "own_agent": str(writer.get("agent") or ""),
            "own_user": str(writer.get("user_id") or "-"),
            "own_session": str(writer.get("session_id") or "-"),
        }
        # A write judges and retires only rows inside the writer's read scope.
        in_scope = scope_and(request_scope(), params)
        live = await ch.query_all(
            f"""
            SELECT toString(memory_id) AS memory_id, namespace, agent, user_id, session_id,
                   memory_key, value, pinned, valid_at, created_at, updated_at, version
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_key IN {{keys:Array(String)}} AND user_id = {{own_user:String}}
              -- the anonymous user is one subject per agent
              AND (user_id != '-' OR agent = {{own_agent:String}})
              AND tombstoned_at IS NULL AND pinned = 0 AND sensitivity != 'high'
              AND status IN ('active', 'duplicate', 'conflicted')
              AND (expires_at IS NULL OR expires_at > now())
              AND (valid_to IS NULL OR valid_to > now())
              AND toString(memory_id) NOT IN {{ids:Array(String)}}
              -- the writer's own stored row is the one this write revises
              AND NOT (agent = {{own_agent:String}} AND session_id = {{own_session:String}})
              {in_scope}
            ORDER BY least(coalesce(valid_at, updated_at), updated_at) DESC, updated_at DESC,
                     toString(memory_id) DESC
            LIMIT {{n:UInt32}} BY memory_key
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
            out.new_rows = await _still_winning(org, namespace, out, params, ch=ch)
        if out.new_rows:
            stamp_new_row_retirements(memory_rows, embedding_rows, out.new_rows, now)
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


def _closed(at: Any) -> bool:
    """Whether a row's expiry or validity end has passed: it is not current."""
    when = _utc(at)
    return when is not None and when <= naive_utc_now()


async def _still_winning(
    org: str, namespace: str, out: JudgedRetirements, peer_params: Mapping[str, Any], *, ch: Any
) -> list[tuple[str, str]]:
    """The new rows whose winner is still the row the judge read: the same version,
    live, unexpired and in its validity window (the judge's LLM call takes seconds,
    and a revision in that time may say something else). A new row whose winner
    changed lands live; the next write of the key judges it again."""
    winners = sorted({w for _n, w in out.new_rows})
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS mid, version
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)} AND user_id = {own_user:String}
          AND memory_id IN {ids:Array(UUID)}
          AND tombstoned_at IS NULL
          AND status IN ('active', 'duplicate', 'conflicted')
          AND (expires_at IS NULL OR expires_at > now())
          AND (valid_to IS NULL OR valid_to > now())
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": org,
            "ns": namespace,
            "keys": peer_params["keys"],
            "own_user": peer_params["own_user"],
            "ids": winners,
        },
    )
    current = {str(r["mid"]): r.get("version") for r in rows}
    return [
        (n, w)
        for n, w in out.new_rows
        if w in current and str(current[w]) == str(out.winner_versions.get(w))
    ]


async def retire_judged_peers(
    org: str, victims: list[dict[str, Any]], *, ch: Any
) -> list[dict[str, Any]]:
    """After the insert, retire the stored rows the judge found older than a row of
    this write. A row locked, pinned, retired, revised or expired since the judge read
    it stays. Returns the victims whose retirement reached `loom.memory`, even if the
    embeddings mirror then failed. Best effort."""
    judged_version = {str(v["memory_id"]): v.get("judged_version") for v in victims}

    def changed(row: memory_repo.MemoryRow) -> bool:
        want = judged_version.get(str(row.memory_id))
        return (
            lock.refuses_retirement(row)
            or (want is not None and int(row.version) != int(want))
            or _closed(row.expires_at)
            or _closed(row.valid_to)
        )

    tracker = RetirementTracker(changed)
    if victims:
        try:
            # Read-your-writes: a lagging replica would pass the version check.
            await memory_repo.supersede_duplicates(
                org,
                victims,
                ch=ch,
                skip=tracker.skip,
                strong=True,
                on_written=tracker.written,
                on_unconfirmed=tracker.unconfirmed,
            )
        except Exception as e:  # noqa: BLE001 — the rows are in; retirement is best effort
            log.warning("same-key judged retirement failed (non-fatal): %s", e)
    return tracker.retired(victims)


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
            policy = await conflicts.namespace_conflict_policy(org, namespace, ch=ch)
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
        # The tracker also refuses a row retired since the read (it is not re-pointed).
        tracker = RetirementTracker(lock.refuses_retirement)
        try:
            n = await memory_repo.supersede_duplicates(
                org,
                victims,
                ch=ch,
                skip=tracker.skip,
                on_written=tracker.written,
                on_unconfirmed=tracker.unconfirmed,
            )
        finally:
            # Beside the response: a point write does not wait on the record. What
            # reached loom.memory is recorded even when the embeddings mirror failed.
            schedule_auto_supersessions(
                org=org,
                namespace=namespace,
                retired=[
                    {**v, "superseded_by_key": v["memory_key"]} for v in tracker.retired(victims)
                ],
                reason=SAME_KEY_NEWEST_WINS,
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
