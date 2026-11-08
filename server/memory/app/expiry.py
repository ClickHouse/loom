"""Expiry has a post-action.

Without one, an expired row would only stop matching the read filter — no
tombstone (so no restore window), no audit row, no way to say "keep the fact,
drop the detail". `on_expire` picks one of three:

    tombstone  (default, '' or 'tombstone') — the row becomes a normal
               tombstone version: memory.restore works for the grace
               window, history shows when and why it went.
    demote     — the expiry is cleared and confidence is scaled by
               `demote_factor` (floor `demote_floor`); the row stays
               retrievable, ranked lower. "Getting stale" without "gone".
    anonymize  — the value is replaced by a marker, entities dropped, the
               embeddings tombstoned so search stops returning it; the shell
               row stays so the fact's existence (key, tags, timestamps,
               provenance) is still provable. "Forget the detail, keep that
               there was one."

The sweep runs as a worker (`memory_expiry_sweep`) and as an ADMIN op
(`memory.sweep_expired`, with dry_run). Between passes the read filter
`(expires_at IS NULL OR expires_at > now())` stays authoritative, so an
expired row is never visible to get/search whatever the sweep's lag —
the same posture as ClickHouse TTL, whose deletes are lazy too.

Locks are honoured: tombstone is a `delete`, demote and anonymize are `value`
writes; a mode that refuses the class leaves the row hidden but untouched,
counted under `locked_skipped`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.errors import caller_text
from server.core.hashing import sha256_value as _sha
from server.memory.app import webhooks
from server.memory.domain import lock as _lock
from server.memory.store.rows import IdentityKey, MemoryRow, identity_key

log = logging.getLogger(__name__)

ACTIONS: tuple[str, ...] = ("tombstone", "demote", "anonymize")
DEFAULT_ACTION = "tombstone"
ANONYMIZED_VALUE = "[EXPIRED]"

# Which lock class each action is, for memory_lock.assert_unlocked.
_LOCK_CLASS: dict[str, str] = {"tombstone": "delete", "demote": "value", "anonymize": "value"}


def effective_action(row: Any) -> str:
    a = str(getattr(row, "on_expire", "") or "")
    return a if a in ACTIONS else DEFAULT_ACTION


@dataclass
class ExpiryConfig:
    batch_size: int = 500
    demote_factor: float = 0.5
    demote_floor: float = 0.05
    org_filter: str | None = None
    namespace_filter: str | None = None
    dry_run: bool = False


@dataclass
class ExpiryResult:
    scanned: int = 0
    tombstoned: int = 0
    demoted: int = 0
    anonymized: int = 0
    locked_skipped: int = 0
    moved_skipped: int = 0  # candidates whose head moved on since ClickHouse's copy
    dry_run: bool = False
    # Up to 20 handled rows: (identity key, the entry as_dict reports).
    sampled: list[tuple[IdentityKey, dict[str, Any]]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def sample(self) -> list[dict[str, Any]]:
        return [entry for _, entry in self.sampled]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "tombstoned": self.tombstoned,
            "demoted": self.demoted,
            "anonymized": self.anonymized,
            "locked_skipped": self.locked_skipped,
            "moved_skipped": self.moved_skipped,
            "dry_run": self.dry_run,
            "sample": self.sample,
            "failures": self.failures,
        }


def _still_expired(head: MemoryRow, now: datetime) -> bool:
    """Whether a head newer than the candidate read is still a live row past its expiry
    (only its copy in ClickHouse lags), so the pass expires it now."""
    if head.tombstoned_at is not None or head.expires_at is None:
        return False
    return to_naive_utc(head.expires_at) <= now


def plan(row: Any, *, now: datetime, cfg: ExpiryConfig) -> dict[str, Any]:
    """Column overrides for the version row that applies `row`'s post-action."""
    action = effective_action(row)
    reason = str(getattr(row, "expire_reason", "") or "")
    if action == "tombstone":
        return {"tombstoned_at": now, "expires_at": None, "source_trace": ""}
    if action == "demote":
        conf = float(getattr(row, "confidence", 1.0) or 0.0)
        new_conf = max(cfg.demote_floor, min(1.0, conf * cfg.demote_factor))
        return {
            "expires_at": None,
            "confidence": round(new_conf, 4),
            "on_expire": "",
            "expire_reason": (f"demoted on expiry: {reason}" if reason else "demoted on expiry")[
                :512
            ],
        }
    # anonymize
    return {
        "value": json.dumps(ANONYMIZED_VALUE),
        "value_json": "{}",
        "content_sha256": _sha(ANONYMIZED_VALUE),
        "entities": [],
        "expires_at": None,
        "on_expire": "",
        "redacted_at": now,
        "redaction_reason": (f"expired:anonymize|{reason}" if reason else "expired:anonymize")[
            :512
        ],
    }


def emit_sweep_event(res: ExpiryResult, *, latency_ms: int, source: str = "worker") -> None:
    """One `agent_events` row per sweep pass (kind='worker', operation
    memory.sweep_expired) so the sweep is observable where every other
    mutation is. Without it a day with zero on_expire rows would be
    indistinguishable from a worker that never ran.
    Counts ride the args map as identifiers; never raises."""
    try:
        from datetime import UTC, datetime

        from server.observability.events import AgentEvent, get_emitter

        args = {
            "scanned": str(res.scanned),
            "tombstoned": str(res.tombstoned),
            "demoted": str(res.demoted),
            "anonymized": str(res.anonymized),
            "locked_skipped": str(res.locked_skipped),
            "moved_skipped": str(res.moved_skipped),
            "failed": str(len(res.failures)),
            "dry_run": "true" if res.dry_run else "false",
            "source": source,
        }
        get_emitter().emit(
            AgentEvent(
                ts=datetime.now(UTC),
                org="-",
                namespace="-",
                agent="-",
                user_id="-",
                session_id="-",
                operation="memory.sweep_expired",
                request_id="",
                latency_ms=int(latency_ms),
                success=not res.failures,
                error_code="" if not res.failures else "PARTIAL",
                kind="worker",
                args=args,
            )
        )
    except Exception:  # the ledger event is telemetry; the pass result stands
        log.debug("memory_expiry_sweep: event emit failed", exc_info=True)


async def run_pass(cfg: ExpiryConfig | None = None) -> ExpiryResult:
    """One sweep: fetch expired live rows, write each one's post-action as a
    version row (one batched insert), mirror onto the embeddings table."""
    import server.memory.store as memory_repo
    from server.core.ch import get_client
    from server.workers.status import get_registry

    cfg = cfg or ExpiryConfig()
    res = ExpiryResult(dry_run=cfg.dry_run)
    started = time.monotonic()
    try:
        rows = await memory_repo.find_expired_candidates(
            batch_size=cfg.batch_size,
            org_filter=cfg.org_filter,
            namespace_filter=cfg.namespace_filter,
        )
        # The candidates are ClickHouse's: each is expired from the head the authority
        # holds, never from a stale copy; a head renewed or deleted since is left alone.
        now = naive_utc_now()
        current = await memory_repo.current_heads(rows, lambda _, h: _still_expired(h, now))
        res.moved_skipped = len(rows) - len(current)
        res.scanned += res.moved_skipped
        rows = current
    except Exception as e:  # the pass records a failure; the next pass rescans
        log.exception("memory_expiry_sweep: scan failed")
        get_registry().record_failure(
            "memory_expiry_sweep",
            duration_ms=int((time.monotonic() - started) * 1000),
            exc=e,
        )
        # `memory.sweep_expired` returns the failures: the kind; the text is logged above.
        res.failures.append(f"scan: {caller_text(e)}")
        return res

    version_rows: list[dict[str, Any]] = []
    mirrors: list[tuple[str, Any, int]] = []  # (action, row, version) for the embeddings side
    for row in rows:
        res.scanned += 1
        action = effective_action(row)
        mode = _lock.effective_mode(row, now=now)
        if mode and _LOCK_CLASS[action] in _lock.REFUSES[mode]:
            res.locked_skipped += 1
            continue
        overrides = plan(row, now=now, cfg=cfg)
        if len(res.sampled) < 20:
            res.sampled.append(
                (
                    identity_key(row),
                    {
                        "memory_key": row.memory_key,
                        "namespace": row.namespace,
                        "agent": row.agent,
                        "action": action,
                        "expired_at": row.expires_at,
                    },
                )
            )
        if action == "tombstone":
            res.tombstoned += 1
        elif action == "demote":
            res.demoted += 1
        else:
            res.anonymized += 1
        if cfg.dry_run:
            continue
        # One version for the memory row and its vector retire, so a late copy of an
        # older read (read + 1) stays below the retire.
        version = memory_repo.version_over(row)
        version_rows.append(memory_repo.build_memory_version_row(row, version=version, **overrides))
        mirrors.append((action, row, version))

    if version_rows:
        ch = get_client()
        try:
            version_rows, mirrors = await _over_unmoved_heads(version_rows, mirrors, res, ch=ch)
        except Exception as e:  # the pass records a failure; the rows expire next pass
            log.exception("memory_expiry_sweep: version insert failed")
            res.failures.append(f"insert: {caller_text(e)}")
            get_registry().record_failure(
                "memory_expiry_sweep",
                duration_ms=int((time.monotonic() - started) * 1000),
                exc=e,
            )
            return res
        # A demoted or anonymized row is a new version of a live key.
        await webhooks.announce(
            [
                ("memory.tombstoned" if action == "tombstone" else "memory.revised", row)
                for row, (action, _r, _v) in zip(version_rows, mirrors, strict=True)
            ]
        )
        items = _mirror_items(mirrors, cfg=cfg)
        try:
            await apply_mirror(items, ch=ch)
        except Exception as e:  # noqa: BLE001 — the memory rows are authoritative
            # The next pass never selects these keys: the repair replays the record.
            log.warning(
                "memory_expiry_sweep: embeddings mirror failed for %d keys: %s", len(items), e
            )
            res.failures.append(f"mirror:{len(items)} keys: {type(e).__name__}")
            await _record_mirror_failures(items, e)
    outcome = res.as_dict()
    outcome.pop("sample", None)
    get_registry().record_success(
        "memory_expiry_sweep",
        duration_ms=int((time.monotonic() - started) * 1000),
        outcome=outcome,
    )
    return res


def _demote_overrides(row: Any, cfg: ExpiryConfig) -> dict[str, Any]:
    """What demote changes on the key's embedding rows, copied at the memory row's
    version by `apply_mirror`. Tombstone and anonymize retire the vectors instead."""
    conf = max(cfg.demote_floor, min(1.0, float(row.confidence or 0.0) * cfg.demote_factor))
    return {"expires_at": None, "confidence": round(conf, 4)}


async def _over_unmoved_heads(
    version_rows: list[dict[str, Any]],
    mirrors: list[tuple[str, Any, int]],
    res: ExpiryResult,
    *,
    ch: Any,
) -> tuple[list[dict[str, Any]], list[tuple[str, Any, int]]]:
    """The rows written over the heads the pass read, and those that landed. Under the
    Postgres authority a head that moved since refuses the batch: the heads are read
    again and the batch goes once more without the moved ones; refused again (a head
    moved meanwhile, or redacted in place), it is left whole. What is left the next pass
    sees (`moved_skipped`)."""
    import server.memory.store as memory_repo
    from server.memory.store import pg_memory

    reads = [row for _, row, _ in mirrors]
    try:
        await memory_repo.insert_versions_over_heads(version_rows, reads, ch=ch)
        return version_rows, mirrors
    except pg_memory.AdmissionContendedError:
        heads = await memory_repo.authority_heads(reads)
    kept = {
        i
        for i, (read, head) in enumerate(zip(reads, heads, strict=True))
        if head is not None and pg_memory.unchanged(read, head)
    }
    _leave(res, [m for i, m in enumerate(mirrors) if i not in kept])
    version_rows = [r for i, r in enumerate(version_rows) if i in kept]
    mirrors = [m for i, m in enumerate(mirrors) if i in kept]
    if not version_rows:
        return [], []
    try:
        await memory_repo.insert_versions_over_heads(
            version_rows, [row for _, row, _ in mirrors], ch=ch
        )
    except pg_memory.AdmissionContendedError:
        _leave(res, mirrors)
        return [], []
    return version_rows, mirrors


def _leave(res: ExpiryResult, mirrors: list[tuple[str, Any, int]]) -> None:
    """Count these rows as left for the next pass, not as done, and list none of them."""
    left = set()
    for action, row, _ in mirrors:
        res.moved_skipped += 1
        if action == "tombstone":
            res.tombstoned -= 1
        elif action == "demote":
            res.demoted -= 1
        else:
            res.anonymized -= 1
        left.add(identity_key(row))
    res.sampled = [(key, entry) for key, entry in res.sampled if key not in left]


# One key's mirror: its identity key, the memory row's version, the action, and for a
# demote the embedding overrides (None otherwise).
MirrorItem = tuple[IdentityKey, int, str, dict[str, Any] | None]


def _mirror_items(mirrors: list[tuple[str, Any, int]], *, cfg: ExpiryConfig) -> list[MirrorItem]:
    """What each written row's post-action changes on the search side. A demoted 'high'
    head keeps no vector, so it has nothing to mirror."""
    items: list[MirrorItem] = []
    for action, row, version in mirrors:
        if action != "demote":
            items.append((identity_key(row), version, action, None))
        elif row.sensitivity != "high":
            items.append((identity_key(row), version, action, _demote_overrides(row, cfg)))
    return items


async def apply_mirror(items: list[MirrorItem], *, ch: Any | None = None) -> int:
    """Search reads loom.memory_embeddings, so the post-action lands there too, at the
    memory row's version: only rows below it change, never one a later write landed
    above it (entity_vectors has no version: its cutoff is that version's second).
    Tombstone retires the key's vectors (memory_embeddings and entity_vectors), keeping
    the vector in the tombstone for `memory.restore`. Anonymize retires them bare, with
    no text or vector, so an anonymized key keeps no entity names in search and one
    landing after a redaction's purge carries nothing. Demote copies them with the
    expiry cleared and confidence lowered. Batched as one keyed read and one insert per
    500 keys, never one per key."""
    import server.memory.store as memory_repo

    def pins(action: str) -> list[tuple[IdentityKey, int]]:
        return [(key, version) for key, version, a, _copy in items if a == action]

    copies = [
        (key, {**copy, "version": version})
        for key, version, action, copy in items
        if action == "demote" and copy is not None
    ]
    n = 0
    if tombstoned := pins("tombstone"):
        n += await memory_repo.retire_vectors_for_keys(tombstoned, ch=ch)
    if anonymized := pins("anonymize"):
        n += await memory_repo.retire_vectors_for_keys(anonymized, ch=ch, bare=True)
    if copies:
        n += await memory_repo.reemit_embedding_rows_for_keys(copies, ch=ch)
    return n


# A recorded mirror: identity key, the memory row's version, the action.
RecordedMirror = tuple[IdentityKey, int, str]


async def replay_mirror(recorded: list[RecordedMirror], *, ch: Any | None = None) -> int:
    """The mirror of a recorded failure, run after later writes may have landed. A
    tombstone or anonymize runs as recorded (`apply_mirror`): the key's vectors below
    the version hold the text it took out. A demote copies the key's live head as it is
    now, its expiry and confidence, onto the key's live vectors as a metadata copy at
    read + 1: a write since the sweep (a touch, a verify, a tag edit) keeps what it
    set, and a row its writer lands after the read is newer than the copy. A key with
    no live head, or a 'high' one, has nothing to copy."""
    import server.memory.store as memory_repo

    n = await apply_mirror(
        [(key, version, action, None) for key, version, action in recorded if action != "demote"],
        ch=ch,
    )
    demoted = [key for key, _version, action in recorded if action == "demote"]
    if not demoted:
        return n
    heads = await memory_repo.memory_rows_by_identity_keys(demoted, ch=ch, sequential=True)
    copies = [
        (key, {"expires_at": head.expires_at, "confidence": head.confidence})
        for key, head in heads.items()
        if head.sensitivity != "high"
    ]
    if copies:
        n += await memory_repo.reemit_embedding_rows_for_keys(copies, ch=ch)
    return n


async def _record_mirror_failures(items: list[MirrorItem], error: Exception) -> None:
    """One `memory_expiry_mirror` projection failure per writer (org, namespace, agent,
    user_id), its items in the payload, so the repair replays each as one batch and a
    user erasure finds it by `user_id`."""
    from server.memory.app.projection_failures import (
        EXPIRY_MIRROR,
        insert_projection_failures,
        projection_failure_row,
    )

    by_writer: dict[tuple[str, ...], list[MirrorItem]] = {}
    for item in items:
        key = item[0]
        writer = key[:4]  # org, namespace, agent, user_id
        by_writer.setdefault(writer, []).append(item)
    rows = []
    for (org, namespace, agent, user_id), group in by_writer.items():
        rows.append(
            projection_failure_row(
                org=org,
                namespace=namespace,
                agent=agent,
                projection=EXPIRY_MIRROR,
                source_operation="memory.sweep_expired",
                memory_keys=[key[5] for key, *_rest in group],
                error=error,
                payload={
                    "items": [[list(key), version, action] for key, version, action, _c in group]
                },
                user_id=user_id,
            )
        )
    try:
        await insert_projection_failures(rows)
    except Exception as e:  # noqa: BLE001 — the pass reports the mirror failure either way
        log.warning("memory_expiry_sweep: recording %d mirror failures failed: %s", len(rows), e)
