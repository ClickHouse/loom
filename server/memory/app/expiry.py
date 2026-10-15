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

from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value as _sha
from server.memory.domain import lock as _lock
from server.memory.store.inserts import insert_memory_versions

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
    dry_run: bool = False
    sample: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "tombstoned": self.tombstoned,
            "demoted": self.demoted,
            "anonymized": self.anonymized,
            "locked_skipped": self.locked_skipped,
            "dry_run": self.dry_run,
            "sample": self.sample,
            "failures": self.failures,
        }


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
    except Exception as e:  # the pass records a failure; the next pass rescans
        log.exception("memory_expiry_sweep: scan failed")
        get_registry().record_failure(
            "memory_expiry_sweep",
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(e).__name__}: {e}",
        )
        res.failures.append(f"scan: {type(e).__name__}: {e}")
        return res

    now = naive_utc_now()
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
        if len(res.sample) < 20:
            res.sample.append(
                {
                    "memory_key": row.memory_key,
                    "namespace": row.namespace,
                    "agent": row.agent,
                    "action": action,
                    "expired_at": row.expires_at,
                }
            )
        if cfg.dry_run:
            _count(res, action)
            continue
        # One version for the memory row and its vector retire, so a late copy of an
        # older read (read + 1) stays below the retire.
        version = memory_repo.next_version()
        version_rows.append(memory_repo.build_memory_version_row(row, version=version, **overrides))
        mirrors.append((action, row, version))

    if version_rows:
        ch = get_client()
        kept: set[tuple[str, ...]] = set()

        async def _keep(rows: Any) -> None:
            kept.update(_ident(r) for r in rows)

        try:
            # Declared as read: a key touched or locked since is left out, not swept.
            with memory_repo.reading([m[1] for m in mirrors]):
                await insert_memory_versions(version_rows, ch=ch, alongside=[_keep])
        except Exception as e:  # the pass records a failure; the rows expire next pass
            log.exception("memory_expiry_sweep: version insert failed")
            res.failures.append(f"insert: {type(e).__name__}: {e}")
            get_registry().record_failure(
                "memory_expiry_sweep",
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(e).__name__}: {e}",
            )
            return res
        # A row the journal left out (a key locked since, a later write) changed nothing.
        mirrors = [m for m in mirrors if _ident(m[1]) in kept]
        for action, _row, _v in mirrors:
            _count(res, action)
        try:
            await _mirror_embeddings_batch(mirrors, cfg=cfg)
        except Exception as e:  # noqa: BLE001 — the memory rows are authoritative
            # The memory-table rows are authoritative and their read filter
            # already hid them; a stray embedding is the known, tolerated
            # failure mode (same as memory.consolidate). The next pass that
            # sees the key re-mirrors it.
            log.warning(
                "memory_expiry_sweep: embeddings mirror failed for %d keys: %s", len(mirrors), e
            )
            res.failures.append(f"mirror:{len(mirrors)} keys: {type(e).__name__}")
    outcome = res.as_dict()
    outcome.pop("sample", None)
    get_registry().record_success(
        "memory_expiry_sweep",
        duration_ms=int((time.monotonic() - started) * 1000),
        outcome=outcome,
    )
    return res


def _count(res: Any, action: str) -> None:
    """One expiry the pass made (or, dry, would make)."""
    if action == "tombstone":
        res.tombstoned += 1
    elif action == "demote":
        res.demoted += 1
    else:
        res.anonymized += 1


def _ident(r: Any) -> tuple[str, ...]:
    """A row's full identity: one sweep spans organizations and namespaces."""
    cols = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
    return tuple(str(r[c] if isinstance(r, dict) else getattr(r, c)) for c in cols)


def _demote_overrides(row: Any, cfg: ExpiryConfig) -> dict[str, Any]:
    """What demote changes on the key's embedding rows: a metadata copy (read + 1).
    Tombstone and anonymize retire through `retire_vectors_for_keys` instead."""
    conf = max(cfg.demote_floor, min(1.0, float(row.confidence or 0.0) * cfg.demote_factor))
    return {"expires_at": None, "confidence": round(conf, 4)}


async def _mirror_embeddings_batch(
    mirrors: list[tuple[str, Any, int]], *, cfg: ExpiryConfig
) -> int:
    """Search reads loom.memory_embeddings, so the post-action lands there too:
    tombstone and anonymize retire the keys' vectors (memory_embeddings and
    entity_vectors, at the memory row's version, so an anonymized key keeps no entity
    names in search either); demote clears their expiry and lowers confidence.
    Batched as one keyed read and one insert per 500 keys, never one per key."""
    import server.memory.store as memory_repo

    if not mirrors:
        return 0
    retires = [
        ((row.org, row.namespace, row.agent, row.user_id, row.session_id, row.memory_key), version)
        for action, row, version in mirrors
        if action in ("tombstone", "anonymize")
    ]
    copies = [
        (
            (row.org, row.namespace, row.agent, row.user_id, row.session_id, row.memory_key),
            _demote_overrides(row, cfg),
        )
        for action, row, _version in mirrors
        if action not in ("tombstone", "anonymize")
        and row.sensitivity != "high"  # a 'high' head keeps no vector
    ]
    n = await memory_repo.retire_vectors_for_keys(retires) if retires else 0
    if copies:
        n += await memory_repo.reemit_embedding_rows_for_keys(copies)
    return n
