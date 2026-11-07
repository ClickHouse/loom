"""Orphan-vector reconcile worker.

The integrity gauge `loom.integrity.orphan_vectors` counts embedding rows
that are live while their `loom.memory` row is tombstoned or gone. Search
reads `loom.memory_embeddings` on its own predicates and never joins the
memory table, so every such row is a deleted memory that can still come
back as an ANN candidate; `_ann_entity_boosts` reads `loom.entity_vectors`
the same way. Most memories tombstoned by dedup/conflict resolution can be
left with a live embedding when nothing acts on the gauge.

This pass heals the gauge instead of only reading it:

  1. find up to `max_keys_per_pass` live embedding identities whose memory
     row is not live (same ANTI JOIN shape as the probe), older than the
     grace period so an in-flight write is never mistaken for a deletion;
  2. re-emit those identities' embedding rows with `tombstoned_at` set
     (`memory_repo.reemit_embedding_rows_for_keys`: one keyed read and one
     insert per 500 identities; a new RMT version, every column carried, so
     the ReplacingMergeTree collapses onto a tombstone, which a read under
     FINAL sees instead of the vector);
  3. re-emit the same keys' `loom.entity_vectors` rows with `tombstoned_at`
     set, per namespace (`memory_repo.tombstone_entity_vectors_for_keys`, with
     the pass's version as the cutoff: a row created in a later second is
     kept). That table has no session column, so another session's live write
     of the same key made before the pass loses its entity vectors (ranking
     only). It is `ReplacingMergeTree(created_at)`, so
     the re-emit carries `created_at` unchanged: equal versions collapse to
     the last inserted row, and the row stays in its partition. No mutation
     anywhere in this pass (tombstones are never written with ALTER UPDATE).

  4. re-emit the live embedding rows whose `verified` mirror disagrees with
     their memory row's attestation (memory.verify mirrors it after its
     answer, so a crash in between leaves the ranking signal wrong), at the
     pass's version, so a verify or revoke landing after the scan wins.

Idempotent: a healed identity leaves the ANTI JOIN, so re-running is a
fixed point. The cascade at the source (`tombstone_cascade_by_ids`) means
this should find nothing on a healthy deployment; the count it heals is the
gauge's backlog and is recorded per pass.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient
from server.workers.status import log_pass_errors, record_pass

log = logging.getLogger(__name__)


@dataclass
class OrphanReconcileResult:
    orphans_seen: int = 0
    embeddings_healed: int = 0  # embedding rows re-emitted with a tombstone
    identities_healed: int = 0
    entity_vectors_healed: int = 0  # entity_vectors rows re-emitted with a tombstone
    verified_healed: int = 0  # embedding rows re-emitted with the memory row's attestation
    errors: list[str] = field(default_factory=list)


def orphan_identities_sql() -> str:
    """Live embedding identities whose memory row is tombstoned or absent.

    Mirrors the `orphan_vectors` probe in `server/observability/otel_probes.py`,
    plus the grace window and a bound. `FINAL` on both sides so a key with
    an unmerged tombstone version is judged on its latest version."""
    return """
        SELECT e.org AS org, e.namespace AS namespace, e.agent AS agent,
               e.user_id AS user_id, e.session_id AS session_id,
               e.memory_key AS memory_key
        FROM (SELECT org, namespace, agent, user_id, session_id, memory_key,
                     max(created_at) AS created_at
              FROM loom.memory_embeddings FINAL
              WHERE tombstoned_at IS NULL AND embedding_dim > 0
              GROUP BY org, namespace, agent, user_id, session_id, memory_key) AS e
        LEFT ANTI JOIN (SELECT org, namespace, agent, user_id, session_id, memory_key
                        FROM loom.memory FINAL
                        WHERE tombstoned_at IS NULL) AS m
          ON e.org = m.org AND e.namespace = m.namespace
             AND e.agent = m.agent AND e.user_id = m.user_id
             AND e.session_id = m.session_id AND e.memory_key = m.memory_key
        WHERE e.created_at < now() - INTERVAL {grace:UInt32} SECOND
        ORDER BY e.org, e.namespace, e.memory_key
        LIMIT {limit:UInt32}
    """


def verified_drift_sql() -> str:
    """Live embedding identities whose `verified` mirror disagrees with the live
    memory row's attestation, for memory versions older than the grace cutoff
    (an in-flight memory.verify mirror is not drift)."""
    from server.memory.store.sql import attested_sql

    return f"""
        SELECT m.org AS org, m.namespace AS namespace, m.agent AS agent,
               m.user_id AS user_id, m.session_id AS session_id,
               m.memory_key AS memory_key, m.attested AS attested
        FROM (SELECT org, namespace, agent, user_id, session_id, memory_key,
                     min(verified) AS vmin, max(verified) AS vmax
              FROM loom.memory_embeddings FINAL
              WHERE tombstoned_at IS NULL AND embedding_dim > 0
              GROUP BY org, namespace, agent, user_id, session_id, memory_key) AS e
        INNER JOIN (SELECT org, namespace, agent, user_id, session_id, memory_key,
                           toUInt8({attested_sql()}) AS attested
                    FROM loom.memory FINAL
                    WHERE tombstoned_at IS NULL AND version < {{cutoff:UInt64}}) AS m
          ON e.org = m.org AND e.namespace = m.namespace
             AND e.agent = m.agent AND e.user_id = m.user_id
             AND e.session_id = m.session_id AND e.memory_key = m.memory_key
        WHERE e.vmin != m.attested OR e.vmax != m.attested
        ORDER BY m.org, m.namespace, m.memory_key
        LIMIT {{limit:UInt32}}
    """


async def _heal_verified_drift(
    ch: _CHClient, result: OrphanReconcileResult, *, version: int, grace: int, limit: int
) -> None:
    import server.memory.store as memory_repo

    cutoff = max(0, version - grace * 1_000_000_000)
    try:
        rows = await ch.query_all(verified_drift_sql(), params={"cutoff": cutoff, "limit": limit})
        items = [
            (
                (
                    str(r["org"]),
                    str(r["namespace"]),
                    str(r["agent"]),
                    str(r["user_id"]),
                    str(r["session_id"]),
                    str(r["memory_key"]),
                ),
                {"verified": int(r["attested"]), "version": version},
            )
            for r in rows
        ]
        if items:
            result.verified_healed += await memory_repo.reemit_embedding_rows_for_keys(items, ch=ch)
    except Exception as e:  # noqa: BLE001 — the drift scan repeats next pass
        result.errors.append(f"verified: {e}")


async def run_pass(ch: _CHClient | None = None) -> OrphanReconcileResult:
    import server.memory.store as memory_repo
    from server.core.repo_base import _default_client
    from server.settings import settings

    ch = ch or _default_client()
    result = OrphanReconcileResult()
    limit = int(settings.orphan_vector_reconcile_max_keys_per_pass)
    grace = max(0, int(settings.orphan_vector_reconcile_grace_seconds))
    now = naive_utc_now()
    # Drawn before the scan: the retire must outrank a late copy of a row the scan
    # saw (read + 1), and a row written after the scan is newer and is kept.
    version = memory_repo.next_version()

    with record_pass("orphan_vector_reconcile") as rec:
        try:
            rows = await ch.query_all(
                orphan_identities_sql(), params={"grace": grace, "limit": limit}
            )
        except Exception as e:  # noqa: BLE001 — a failed scan ends the pass; the next one retries
            result.errors.append(f"scan: {e}")
            log_pass_errors(log, "orphan_vector_reconcile", result.errors)
            rec.outcome = _outcome(result)
            return result
        result.orphans_seen = len(rows)

        # One keyed FINAL read and one committed insert per 500 identities
        # (`reemit_embedding_rows_for_keys`), not one of each per identity.
        keys = [memory_repo.identity_key(r) for r in rows]
        by_ns: dict[tuple[str, str], list[memory_repo.IdentityKey]] = defaultdict(list)
        try:
            result.embeddings_healed += await memory_repo.reemit_embedding_rows_for_keys(
                [(k, {"tombstoned_at": now, "version": version}) for k in keys], ch=ch
            )
        except Exception as e:  # noqa: BLE001 — the scan repeats next pass; entity vectors wait
            result.errors.append(f"embeddings: {e}")
            keys = []
        for k in keys:
            result.identities_healed += 1
            by_ns[(k[0], k[1])].append(k)

        # entity_vectors rows created in a later second than the pass's version stay.
        cutoff = memory_repo.version_second(version)
        for (_org, ns), ns_keys in by_ns.items():
            try:
                result.entity_vectors_healed += await memory_repo.tombstone_entity_vectors_for_keys(
                    ns_keys, ts=now, ch=ch, created_by=dict.fromkeys(ns_keys, cutoff)
                )
            except Exception as e:  # noqa: BLE001 — one namespace's failure never stops the rest
                result.errors.append(f"entity_vectors {ns}: {e}")

        await _heal_verified_drift(ch, result, version=version, grace=grace, limit=limit)

        if result.orphans_seen:
            log.info(
                "orphan_vector_reconcile: %d orphan identities, %d embedding rows "
                "and %d entity_vectors rows tombstoned, %d errors",
                result.orphans_seen,
                result.embeddings_healed,
                result.entity_vectors_healed,
                len(result.errors),
            )
        log_pass_errors(log, "orphan_vector_reconcile", result.errors)
        rec.outcome = _outcome(result)
    return result


def _outcome(result: OrphanReconcileResult) -> dict[str, Any]:
    return {
        "orphans_seen": result.orphans_seen,
        "identities_healed": result.identities_healed,
        "embeddings_healed": result.embeddings_healed,
        "entity_vectors_healed": result.entity_vectors_healed,
        "verified_healed": result.verified_healed,
        "errors": len(result.errors),
    }


__all__ = [
    "OrphanReconcileResult",
    "orphan_identities_sql",
    "run_pass",
    "verified_drift_sql",
]
