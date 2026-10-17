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
from datetime import datetime
from typing import Any

import server.memory.store as memory_repo
from server.core.datetimes import parse_iso_dt, to_naive_utc
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.memory.app import conflicts
from server.memory.app.auto_resolution import (
    SAME_KEY_NEWEST_WINS,
    RetirementTracker,
    schedule_auto_supersessions,
)
from server.memory.domain import lock

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


def statement_rank(row: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    """When a row's statement holds, as one total order: its valid time, else when
    it was written (`updated_at`: a revision carries `created_at` forward from the
    key's first version), capped at when it was written (a fact dated in the future
    is still stated when it was written); then when it was written; then its id, an
    arbitrary but fixed tie-break. The SQL that picks the newest peers orders by the
    same key. Times in UTC, to the second."""
    written = _utc(row.get("updated_at")) or _utc(row.get("created_at")) or datetime.min
    valid = _utc(row.get("valid_at")) or written
    return (min(valid, written), written, str(row.get("memory_id") or ""))


def older_statement(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether `a` is the earlier statement of the two (`statement_rank`)."""
    return statement_rank(a) < statement_rank(b)


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


async def supersede_same_key_rows(
    *,
    org: str,
    namespace: str,
    new_rows: list[dict[str, Any]],
    ch: Any,
    policy: str | None = None,
) -> int:
    """Retire every older live row that shares a key with one of `new_rows`.

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
            # Read-your-writes: a lagging replica's row would be rebuilt over a revision.
            n = await memory_repo.supersede_duplicates(
                org,
                victims,
                ch=ch,
                skip=tracker.skip,
                strong=True,
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
