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
     the ReplacingMergeTree collapses onto a tombstone and reads drop the
     vector);
  3. re-emit the same keys' `loom.entity_vectors` rows with `tombstoned_at`
     set, per namespace. That table is `ReplacingMergeTree(created_at)`, so
     the re-emit carries `created_at` unchanged: equal versions collapse to
     the last inserted row, and the row stays in its partition. No mutation
     anywhere in this pass (tombstones are never written with ALTER UPDATE).

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
from server.memory.store.sweeps import entity_vectors_live_sql
from server.workers.status import log_pass_errors, record_pass

log = logging.getLogger(__name__)


@dataclass
class OrphanReconcileResult:
    orphans_seen: int = 0
    embeddings_healed: int = 0  # embedding rows re-emitted with a tombstone
    identities_healed: int = 0
    entity_vectors_healed: int = 0  # entity_vectors rows re-emitted with a tombstone
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


async def run_pass(ch: _CHClient | None = None) -> OrphanReconcileResult:
    import server.memory.store as memory_repo
    from server.core.repo_base import _default_client
    from server.settings import settings

    ch = ch or _default_client()
    result = OrphanReconcileResult()
    limit = int(settings.orphan_vector_reconcile_max_keys_per_pass)
    grace = max(0, int(settings.orphan_vector_reconcile_grace_seconds))
    now = naive_utc_now()

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
        keys: list[memory_repo.EmbeddingKey] = [
            (
                str(r["org"]),
                str(r["namespace"]),
                str(r["agent"]),
                str(r["user_id"]),
                str(r["session_id"]),
                str(r["memory_key"]),
            )
            for r in rows
        ]
        by_ns: dict[tuple[str, str], list[str]] = defaultdict(list)
        try:
            result.embeddings_healed += await memory_repo.reemit_embedding_rows_for_keys(
                [(k, {"tombstoned_at": now}) for k in keys], ch=ch
            )
        except Exception as e:  # noqa: BLE001 — the scan repeats next pass; entity vectors wait
            result.errors.append(f"embeddings: {e}")
            keys = []
        for k in keys:
            result.identities_healed += 1
            by_ns[(k[0], k[1])].append(k[5])

        for (org, ns), ns_keys in by_ns.items():
            try:
                live = await ch.query_all(
                    entity_vectors_live_sql(),
                    params={"org": org, "ns": ns, "keys": ns_keys},
                )
                if live:
                    await ch.insert(
                        "loom.entity_vectors",
                        [{**row, "tombstoned_at": now} for row in live],
                        settings={"async_insert": 0, "wait_for_async_insert": 1},
                    )
                    result.entity_vectors_healed += len(live)
            except Exception as e:  # noqa: BLE001 — one namespace's failure never stops the rest
                result.errors.append(f"entity_vectors {ns}: {e}")

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
        "errors": len(result.errors),
    }


__all__ = [
    "OrphanReconcileResult",
    "orphan_identities_sql",
    "run_pass",
]
