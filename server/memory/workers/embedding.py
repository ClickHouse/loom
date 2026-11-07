"""Embedding recovery worker.

Recovery leg for the synchronous handler write path: scans for stub rows
(`embedding_dim = 0`) left behind by a failed inline embed, computes the
embedding, and writes a replacement row that wins under ReplacingMergeTree
FINAL. `memory.search` filters `embedding_dim > 0`, so stubs are hidden
until filled.

A row whose embed raises deterministically (content-policy, token limit)
accrues `embed_attempts` and retries; after `max_attempts` it's poisoned
(`embed_failed_at` set, skipped by the query filter) so one bad row can't
stall the batch.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import server.memory.store as memory_repo
from server.core.datetimes import utc_now
from server.embedding import get_embedder
from server.llm import customer, identity
from server.memory.app.model_attribution import attribution_for, memory_writers
from server.workers import pass_context
from server.workers.holds import Held, hold_seconds
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingWorkerConfig:
    batch_size: int = 100
    org_filter: str | None = None
    namespace_filter: str | None = None
    # Failed embeds past this count are poisoned and skipped on future passes.
    max_attempts: int = 5


@dataclass
class EmbeddingWorkerResult:
    embedded: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    poisoned: list[dict[str, Any]] = field(default_factory=list)
    # Memories that had NO embedding row and were given a stub this pass.
    reconciled: list[dict[str, Any]] = field(default_factory=list)

    @property
    def embedded_count(self) -> int:
        return len(self.embedded)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def poisoned_count(self) -> int:
        return len(self.poisoned)


async def _refresh_entities(rows: list[dict[str, Any]]) -> None:
    """Overwrite each pending row's `entities` with the CURRENT value from
    loom.memory. Best-effort: on any failure the stale snapshot ships (the
    divergence gauge is the detector), never the batch."""
    if not rows:
        return
    try:
        from server.core.ch import get_client

        keys = list({r["memory_key"] for r in rows})
        fresh = await get_client().query_all(
            """
            SELECT org, namespace, agent, user_id, session_id, memory_key,
                   entities
            FROM loom.memory FINAL
            WHERE memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
            """,
            {"keys": keys},
        )
        by_ident = {
            (
                str(f["org"]),
                str(f["namespace"]),
                str(f["agent"]),
                str(f["user_id"]),
                str(f["session_id"]),
                str(f["memory_key"]),
            ): list(f["entities"] or [])
            for f in fresh
        }
        for r in rows:
            k = (
                str(r["org"]),
                str(r["namespace"]),
                str(r["agent"]),
                str(r["user_id"]),
                str(r["session_id"]),
                str(r["memory_key"]),
            )
            if k in by_ident:
                r["entities"] = by_ident[k]
    except Exception as e:  # noqa: BLE001 — additive freshness, never fatal
        log.debug("embedding worker: entity refresh skipped: %s", e)


def _fenced_version(r: dict[str, Any]) -> int | None:
    """The version a replacement of stub row `r` must carry.

    A fresh write-time version (build_embedding_row's default) makes the
    worker's row the newest for the key no matter when it was read: a
    memory.set that rewrote the memory between the stub SELECT and this
    INSERT loses under FINAL, and the memory is searchable forever by the
    text it no longer has. stub_version + 1 is newer than the stub (so the
    replacement wins over it) and older than any later write (versions are
    nanosecond timestamps, so +1 never overtakes a real later write). A stub
    without a version falls back to the default.
    """
    v = r.get("version")
    try:
        return int(v) + 1 if v else None
    except (TypeError, ValueError):
        return None


def _carry_signals(r: dict[str, Any]) -> dict[str, Any]:
    """Identity + every denormalized column from the stub row.

    memory_embeddings is ReplacingMergeTree(version); a replacement that omits
    any column resurrects its schema default under FINAL. created_at is carried
    verbatim to preserve the memory's true age for recency decay /
    chronological ranking; version is the stub's + 1 (`_fenced_version`) so
    a re-embed never outranks a write that happened after the stub was read.
    """
    return {
        "version": _fenced_version(r),
        "org": r["org"],
        "namespace": r["namespace"],
        "agent": r["agent"],
        "user_id": r["user_id"],
        "session_id": r["session_id"],
        "memory_key": r["memory_key"],
        "memory_type": r["memory_type"],
        "scope": r["scope"],
        "run_id": r.get("run_id") or "-",
        "created_at": r.get("created_at"),
        "entities": list(r.get("entities") or []),
        "tags": list(r.get("tags") or []),
        "valid_at": r.get("valid_at"),
        "expires_at": r.get("expires_at"),
        "valid_to": r.get("valid_to"),
        "invalid_at": r.get("invalid_at"),
        "temporal_anchor": r.get("temporal_anchor"),
        "temporal_confidence": float(r.get("temporal_confidence") or 0.0),
        "reinforcement_count": int(r.get("reinforcement_count") or 1),
        "last_reinforced_at": r.get("last_reinforced_at"),
        "mentioned_at": r.get("mentioned_at"),
        "source_resource_id": r.get("source_resource_id") or "",
        # Provenance + attestation + conflict stamp: ranking
        # reads source_class and verified; omitting them here would reset both
        # to their DEFAULT on every re-embed.
        "source_class": r.get("source_class") or "",
        "verified": int(r.get("verified") or 0),
        "conflict_checked_at": r.get("conflict_checked_at"),
        # The write the row mirrors: the conflict pass tells revisions apart by it.
        "content_sha256": r.get("content_sha256") or "",
        "status": r.get("status") or "active",
        "confidence": float(r.get("confidence") or 1.0),
        "memory_key_embedding": list(r.get("memory_key_embedding") or []),
        "memory_key_embedding_dim": int(r.get("memory_key_embedding_dim") or 0),
        "files": list(r.get("files") or []),
        "concepts": list(r.get("concepts") or []),
    }


async def _embed_rows(
    embedder: Any,
    rows: list[dict[str, Any]],
    writers: dict[tuple[str, ...], str] | None = None,
) -> list[tuple[list[float] | None, str]]:
    """Embed each writer's rows together; held identities retry without spending attempts."""
    out: list[tuple[list[float] | None, str]] = [(None, "")] * len(rows)
    if writers is None:
        writers = await memory_writers(rows) if identity.reads_writer() else {}
    groups = await customer.groups(attribution_for(r, writers) for r in rows)
    for who, positions in groups.items():
        embedded: list[tuple[list[float] | None, str]]
        group = [rows[i] for i in positions]
        remaining = max(
            _HELD_WHO.left(identity.split_by(attribution_for(r, writers))) for r in group
        )
        if remaining > 0:
            _hold(group, writers, remaining)
            for i in positions:
                out[i] = (None, HELD)
            continue
        try:
            with customer.attributed(who):
                if not customer.names_one():
                    embedded = [(None, customer.NO_CUSTOMER)] * len(positions)
                else:
                    texts = [rows[i]["content_excerpt"] for i in positions]
                    embedded = await _embed_batch(embedder, texts)
        except customer.ModelCallsDisabled:
            embedded = [(None, HELD)] * len(positions)
        except customer.GROUP_HOLDS as e:
            _hold([rows[i] for i in positions], writers, hold_seconds(e))
            embedded = [(None, HELD)] * len(positions)
        for i, one in zip(positions, embedded, strict=True):
            out[i] = one
    return out


# Held rows retain their embedding attempts.
HELD = "model call held"

_HELD_WHO: Held[Any] = Held()


def _hold(rows: list[dict[str, Any]], writers: dict[tuple[str, ...], str], seconds: float) -> None:
    """Persist a retry time without spending an embedding attempt."""
    _HELD_WHO.hold((identity.split_by(attribution_for(r, writers)) for r in rows), seconds)
    until = utc_now() + timedelta(seconds=seconds)
    for row in rows:
        row["embed_retry_at"] = until


async def _embed_batch(embedder: Any, texts: list[str]) -> list[tuple[list[float] | None, str]]:
    """`(vector, error)` per input, batching the happy path.

    One HTTP request per batch instead of one per row: `embed_many` sends up to
    100 inputs per request, where single-text calls cost ~242ms each.

    This worker's value is its per-row failure accounting — attempts bumped,
    poison detected, a retry stub re-inserted with that row's own error.
    `embed_many` raises for the whole batch, so batching alone would fail 100
    rows for one bad input and lose every individual error.

    Hence: batch first, and on ANY batch failure fall back to per-text embeds so
    one poison row cannot fail its 99 neighbours and each row still gets its own
    error string. Every vector here is stored, so both paths call `embed_many`
    (the raw text); `embed` is the query path and normalises whitespace.

    A length mismatch from `embed_many` is treated as a hard failure rather than
    zipped: pairing vectors to the wrong rows would attach one memory's embedding
    to another and silently corrupt retrieval, which is far worse than being slow.
    """
    if not texts:
        return []
    if len(texts) > 1:
        try:
            vecs = await embedder.embed_many(texts)
            if len(vecs) == len(texts):
                return [(v, "") for v in vecs]
            log.warning(
                "embedding worker: embed_many returned %d vectors for %d inputs; "
                "refusing to pair them and falling back to per-row embeds",
                len(vecs),
                len(texts),
            )
        except customer.HOLDS:
            raise  # the org is switched off, or may be: no row's attempt is spent
        except Exception as e:  # noqa: BLE001 — batch failure falls back to per-row
            log.info(
                "embedding worker: batch of %d failed (%s: %s); isolating per row",
                len(texts),
                type(e).__name__,
                e,
            )

    out: list[tuple[list[float] | None, str]] = []
    for t in texts:
        try:
            one = await embedder.embed_many([t])
            if len(one) != 1:
                raise RuntimeError(f"embed_many returned {len(one)} vectors for 1 input")
            out.append((one[0], ""))
        except customer.HOLDS:
            raise
        except Exception as e:  # noqa: BLE001 — per-row isolation is the point
            out.append((None, f"{type(e).__name__}: {e}"[:512]))
    return out


async def _stubs_still_served(
    rows: list[dict[str, Any]], stub_embedding: list[float], now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The stubs whose head may still keep a vector, and tombstones (two above the
    stub, its excerpt cleared) for the rest. Shared-scope stubs have no head of
    their own and are kept."""
    from server.memory.store import read_your_writes_enabled

    own = [r for r in rows if str(r.get("scope") or "") != "shared"]
    if not own:
        return rows, []
    sequential = read_your_writes_enabled()
    heads = await memory_repo.head_sensitivities(
        sorted({memory_repo.identity_key(r) for r in own}), sequential=sequential
    )

    def served(r: dict[str, Any]) -> bool:
        if str(r.get("scope") or "") == "shared":
            return True
        head = heads.get(memory_repo.identity_key(r))
        return head != "high" and (head is not None or not sequential)

    keep = [r for r in rows if served(r)]
    dropped = [
        memory_repo.build_embedding_row(
            **{**_carry_signals(r), "version": int(r["version"]) + 2},
            content_excerpt="",
            embedding_model=r["embedding_model"],
            embedding_dim=0,
            embedding=stub_embedding,
            tombstoned_at=now,
        )
        for r in rows
        if not served(r)
    ]
    return keep, dropped


def _replacement_rows(
    r: dict[str, Any],
    vec: list[float],
    embedder: Any,
    stub_embedding: list[float],
    now: datetime,
) -> list[dict[str, Any]]:
    """The embedded row for stub `r`, plus a tombstone of the stub when it is
    labelled with a model other than the served one. embedding_model is in
    the sort key, so the new model's row is a new key: without the tombstone
    the stub stays dim=0 and is re-picked, and re-embedded, every pass."""
    common = {
        **_carry_signals(r),
        "content_excerpt": r["content_excerpt"],
        "embed_attempts": int(r.get("embed_attempts") or 0),
        "embed_last_error": "",
        "embed_failed_at": None,
    }
    rows = [
        memory_repo.build_embedding_row(
            **common,
            embedding_model=embedder.model_name,
            embedding_dim=embedder.dim,
            embedding=vec,
            tombstoned_at=None,
        )
    ]
    if r["embedding_model"] != embedder.model_name:
        rows.append(
            memory_repo.build_embedding_row(
                **common,
                embedding_model=r["embedding_model"],
                embedding_dim=0,
                embedding=stub_embedding,
                tombstoned_at=now,
            )
        )
    return rows


# The orphan reconciliation looks at what just arrived on
# every pass and at everything on a slow clock. Module state, like the
# entity heal's clock: one full scan per interval per
# process, wherever the supervisor runs the pass.
_last_full_orphan_scan: float = 0.0


def _orphan_scan_window(*, now: float | None = None) -> int | None:
    """`None` when this pass owes the full anti-join (first pass, or the
    slow clock has elapsed, or the clock is disabled); otherwise the recent
    window in seconds. Settings: `embedding_worker_orphan_full_scan_seconds`
    (0 = every pass is full) and
    `embedding_worker_orphan_recent_seconds`."""
    global _last_full_orphan_scan
    from server.settings import settings

    full_every = int(settings.embedding_worker_orphan_full_scan_seconds)
    recent = int(settings.embedding_worker_orphan_recent_seconds)
    if full_every <= 0 or recent <= 0:
        return None
    t = time.monotonic() if now is None else now
    if _last_full_orphan_scan <= 0.0 or t - _last_full_orphan_scan >= full_every:
        _last_full_orphan_scan = t
        return None
    return recent


def _outcome(result: EmbeddingWorkerResult, *, idle: bool, rows_written: int) -> dict[str, Any]:
    """The pass's counts for the registry, the pass row and the status row
    (`outcome_<key>`). `idle` — nothing
    arrived, nothing to fill — also marks the pass so the supervisor sleeps
    the idle interval (30 s) instead of the 5 s interval; the first pass
    that writes a row brings the 5 s back."""
    if idle:
        pass_context.mark_idle()
    return {
        "embedded_count": result.embedded_count,
        "failed_count": result.failed_count,
        "poisoned_count": result.poisoned_count,
        "rows_written": rows_written,
        "idle": idle,
    }


async def run_pass(
    cfg: EmbeddingWorkerConfig | None = None,
) -> EmbeddingWorkerResult:
    """Embed all stub rows (one batch up to cfg.batch_size)."""
    cfg = cfg or EmbeddingWorkerConfig()
    result = EmbeddingWorkerResult()

    with record_pass("embedding_worker") as rec:
        # Hoisted above reconciliation: the stub's `embedding_model` must match what
        # this embedder will write, because that column is part of the dedup key.
        embedder = get_embedder()
        # Stubs are marked by embedding_dim=0 (search filters embedding_dim>0
        # and the drain fills them), so the vector value is irrelevant. Empty:
        # the insert pads it to the width a coded column takes.
        stub_embedding: list[float] = []

        # Reconcile memories that have NO embedding row at all, so the stub drain
        # below picks them up. `find_embedding_stubs` only sees `embedding_dim = 0`
        # rows; a memory with no sibling row is invisible to it, so nothing would
        # ever embed it and it would stay unsearchable while the embed gauge reads 0.
        #
        # Reconciling here rather than fixing each writer is deliberate: bulk
        # imports and observation writers can both produce such rows, and the next
        # writer to forget would reintroduce it.
        #
        # `embedding_model=embedder.model_name` is LOAD-BEARING, not cosmetic.
        # memory_embeddings is ReplacingMergeTree ORDER BY
        # (org, namespace, agent, user_id, session_id, scope, embedding_model,
        # memory_key) — `embedding_model` is part of the dedup key. A stub written
        # with a different model than the embed will produce is a DIFFERENT row, so
        # the embedded row never replaces it, the dim=0 row survives forever, and
        # `find_embedding_stubs` re-picks it every pass. Matching the model is
        # what makes convergence structural.
        try:
            orphans = await memory_repo.find_memories_without_embedding_rows(
                batch_size=cfg.batch_size,
                org_filter=cfg.org_filter,
                namespace_filter=cfg.namespace_filter,
                since_seconds=_orphan_scan_window(),
            )
            if orphans:
                await memory_repo.insert_embedding_rows(
                    [
                        memory_repo.build_embedding_row(
                            **{k: v for k, v in o.items() if k != "created_at"},
                            embedding_model=embedder.model_name,
                            embedding_dim=0,
                            embedding=stub_embedding,
                            tombstoned_at=None,
                            embed_attempts=0,
                            embed_last_error="",
                            embed_failed_at=None,
                            created_at=o["created_at"],
                        )
                        for o in orphans
                    ]
                )
                result.reconciled = [
                    {"namespace": o["namespace"], "memory_key": o["memory_key"]} for o in orphans
                ]
                log.warning(
                    "embedding worker: reconciled %d memories that had NO embedding "
                    "row (unsearchable until now)",
                    len(orphans),
                )
        except Exception as e:  # noqa: BLE001 — repair is additive; never break the pass
            # The pass goes on, the supervisor hears about it.
            log.warning("embedding worker: reconciliation pass failed: %s", e)
            pass_context.note_failure(e, "reconciliation")

        rows = await memory_repo.find_embedding_stubs(
            batch_size=cfg.batch_size,
            max_attempts=cfg.max_attempts,
            org_filter=cfg.org_filter,
            namespace_filter=cfg.namespace_filter,
            exclude_orgs=await customer.switched_off_orgs(),
        )

        replacements: list[dict[str, Any]] = []
        retries: list[dict[str, Any]] = []
        # tz-aware: clickhouse-connect reads naive datetimes as client-local.
        now = datetime.now(UTC)
        # A stub whose head is now 'high' (or gone, on a sequential read) is never sent
        # to the embedding provider: its excerpt may hold the high text. It is retired.
        rows, dropped = await _stubs_still_served(rows, stub_embedding, now)
        if dropped:
            await memory_repo.insert_embedding_rows(dropped)
        writers = await memory_writers(rows) if rows and identity.reads_writer() else {}
        if rows:
            embedded = await _embed_rows(embedder, rows, writers)
            for r, (vec, embed_err) in zip(rows, embedded, strict=True):
                content = r["content_excerpt"]
                if vec is None and embed_err == HELD:
                    if r.get("embed_retry_at") is None:
                        continue  # switched-off orgs are excluded by the next selection
                    retries.append(
                        memory_repo.build_embedding_row(
                            **_carry_signals(r),
                            content_excerpt=content,
                            embedding_model=r["embedding_model"],
                            embedding_dim=0,
                            embedding=stub_embedding,
                            embed_attempts=int(r.get("embed_attempts") or 0),
                            embed_last_error=HELD,
                            embed_retry_at=r["embed_retry_at"],
                        )
                    )
                    continue
                if vec is None:
                    new_attempts = int(r.get("embed_attempts") or 0) + 1
                    err = embed_err
                    is_poisoned = new_attempts >= cfg.max_attempts or err == customer.NO_CUSTOMER
                    # Re-insert the stub with bumped failure state (stays embedding_dim=0).
                    retries.append(
                        memory_repo.build_embedding_row(
                            **_carry_signals(r),
                            content_excerpt=content,
                            embedding_model=r["embedding_model"],
                            embedding_dim=0,
                            embedding=stub_embedding,
                            tombstoned_at=None,
                            embed_attempts=new_attempts,
                            embed_last_error=err,
                            embed_failed_at=now if is_poisoned else None,
                        )
                    )
                    target = result.poisoned if is_poisoned else result.failed
                    target.append(
                        {
                            "namespace": r["namespace"],
                            "memory_key": r["memory_key"],
                            "attempts": new_attempts,
                            "error": err,
                        }
                    )
                    log.warning(
                        "embedding worker: %s ns=%s key=%s attempt=%d err=%s",
                        "POISONED" if is_poisoned else "retry",
                        r["namespace"],
                        r["memory_key"],
                        new_attempts,
                        err,
                    )
                    continue
                replacements.extend(_replacement_rows(r, vec, embedder, stub_embedding, now))
                result.embedded.append(
                    {
                        "namespace": r["namespace"],
                        "memory_key": r["memory_key"],
                    }
                )
            # Entities can advance between the stub read and this insert
            # (the entity extractor's apply_entities_batch re-emits version rows);
            # a re-emit built from the stale snapshot would resurrect the
            # old list under FINAL.
            # Re-read at insert time to shrink that window from
            # minutes (embed round-trips) to milliseconds.
            await _refresh_entities(replacements + retries)
            await memory_repo.insert_embedding_rows(replacements)
            # A stub drawn from a read before a 'high' write landed can sit above
            # its fence: the fill re-checks its heads like every vector copy.
            await memory_repo.retire_copies_under_unembedded_heads(replacements)
            # A fill sits at its stub's version + 1, usually far under the BM25 watermark.
            await memory_repo.index_late_rows(replacements)
            await memory_repo.insert_embedding_rows(retries)

        rec.outcome = _outcome(
            result,
            idle=not rows and not result.reconciled,
            rows_written=len(replacements) + len(retries) + len(result.reconciled),
        )

    log.info(
        "embedding worker: embedded=%d failed=%d poisoned=%d",
        result.embedded_count,
        result.failed_count,
        result.poisoned_count,
    )
    return result


__all__ = ["EmbeddingWorkerConfig", "EmbeddingWorkerResult", "run_pass"]
