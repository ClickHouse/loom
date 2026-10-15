"""Temporal projection worker: repairs journal breaks, replays commits the
assertion projection missed, brings loom.memory rows in line with what the journal
says applies now, and fills historical-search vectors
(`server/memory/app/temporal_projection.py`)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from server.embedding import get_embedder
from server.memory.app import temporal_projection as tp
from server.memory.store import temporal as store
from server.workers.status import record_pass

log = logging.getLogger(__name__)


async def run_pass() -> None:
    with record_pass("memory_temporal_projection") as rec:
        failed: list[str] = []
        rec.outcome = {
            "repaired": await _stage("repair", failed, _repair),
            "replayed": await _stage("replay", failed, _replay),
            "current_rows": await _stage("reconcile", failed, tp.reconcile_current),
            "vectors": await _stage("vectors", failed, _fill_vectors),
            "orphan_vectors": await _stage("orphans", failed, store.delete_orphan_vectors),
            # A stage that fails every pass shows here, not as "nothing to do".
            "failed_stages": failed,
        }


async def _stage(name: str, failed: list[str], run: Callable[[], Awaitable[int]]) -> int:
    try:
        return await run()
    except Exception as e:  # noqa: BLE001 — one stage must not stop the others
        log.warning("temporal projection stage %s failed: %r", name, e)
        failed.append(name)
        return 0


async def _repair() -> int:
    repaired = 0
    for r in await store.namespaces_broken():
        try:
            seqs = await store.repair_namespace(str(r["org"]), str(r["namespace"]))
        except Exception as e:  # noqa: BLE001 — one namespace must not stop the others
            log.warning("temporal repair %s/%s failed: %r", r["org"], r["namespace"], e)
            continue
        log.warning("temporal journal %s/%s: repaired sequences %s", r["org"], r["namespace"], seqs)
        repaired += len(seqs)
    return repaired


async def _replay() -> int:
    replayed = 0
    for r in await store.namespaces_behind():
        try:
            applied = await tp.replay(
                str(r["org"]),
                str(r["namespace"]),
                int(r["applied"]),
                int(r["head"]),
                published_at=r.get("published_at"),
            )
        except Exception as e:  # noqa: BLE001 — one namespace must not stop the others
            log.warning("temporal replay %s/%s failed: %r", r["org"], r["namespace"], e)
            continue
        replayed += max(0, applied - int(r["applied"]))
    return replayed


async def _fill_vectors() -> int:
    embedder = get_embedder()
    return await tp.fill_vectors(embedder.model_name, embedder)
