"""namespace_gc worker — a registered bench namespace nobody writes to leaves.

The verb is one list-scoped, purging erasure saga per batch of throwaway
namespaces; `loom namespaces-gc` runs it by hand, and this worker runs it so an
operator need not remember to. Every namespace's first write creates a registry
row with `kind = 'bench'` when its name is under a bench
prefix, so the candidate set is not a prefix guess: it is the registry
rows of kind bench that still hold live memory and have had no write
(`loom.namespace_write_marks`, falling back to the newest memory row) for
`namespace_gc_idle_hours`.

One pass submits at most `namespace_gc_sagas_per_pass` sagas of
`namespace_gc_batch` namespaces each, oldest write first, and awaits each
(the saga polls its mutations by state, so a pass can run for
the mutation wait budget). A saga that does not complete stops the pass;
the ledger row says why and the next pass finds the same candidates.
Nothing here reads a `retention_days`: a bench namespace is never on a TTL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.identity.domain.census import BenchGcCandidate
from server.settings import settings
from server.workers.status import record_pass

log = logging.getLogger(__name__)

REQUESTER = "worker:namespace_gc"


@dataclass(frozen=True)
class GcConfig:
    idle_hours: int = 24
    batch: int = 200
    sagas_per_pass: int = 1
    # The worker keeps the saga to loom tables; `loom namespaces-gc --erase`
    # also reaches the external backends.
    external: bool = False
    purge: bool = True
    # Narrowing for a one-off run from the CLI; the worker leaves both unset.
    org: str | None = None
    namespaces: tuple[str, ...] = ()
    dry_run: bool = False
    requester: str = REQUESTER

    @classmethod
    def from_settings(cls) -> GcConfig:
        return cls(
            idle_hours=int(settings.namespace_gc_idle_hours),
            batch=max(1, int(settings.namespace_gc_batch)),
            sagas_per_pass=max(1, int(settings.namespace_gc_sagas_per_pass)),
        )


@dataclass
class GcResult:
    candidates: int = 0
    candidate_rows: int = 0
    sagas: int = 0
    erased: int = 0
    stopped_on: str = ""
    names: list[str] = field(default_factory=list)


async def run_pass(cfg: GcConfig | None = None, *, ch: Any | None = None) -> GcResult:
    cfg = cfg or GcConfig.from_settings()
    result = GcResult()
    from server.identity.store import namespace_census

    with record_pass("namespace_gc") as rec:
        cands = await namespace_census.bench_gc_candidates(
            idle_hours=cfg.idle_hours,
            org=cfg.org,
            namespaces=cfg.namespaces,
            limit=cfg.batch * cfg.sagas_per_pass,
            ch=ch,
        )
        result.candidates = len(cands)
        result.candidate_rows = sum(c.live_rows for c in cands)
        result.names = [c.namespace for c in cands]
        if cands and not cfg.dry_run:
            await _erase(cands, cfg, result)
        rec.outcome = {
            "candidates": result.candidates,
            "candidate_rows": result.candidate_rows,
            "sagas": result.sagas,
            "erased": result.erased,
            "stopped_on": result.stopped_on,
        }
    if result.candidates:
        log.info(
            "namespace_gc: candidates=%d rows=%d sagas=%d erased=%d%s",
            result.candidates,
            result.candidate_rows,
            result.sagas,
            result.erased,
            f" stopped_on={result.stopped_on}" if result.stopped_on else "",
        )
    return result


def _batches(cands: list[BenchGcCandidate], size: int) -> list[tuple[str, list[str]]]:
    """(org, names) batches — a saga is org-scoped, so the batch is too."""
    by_org: dict[str, list[str]] = {}
    for c in cands:
        by_org.setdefault(c.org, []).append(c.namespace)
    out: list[tuple[str, list[str]]] = []
    for org, names in by_org.items():
        for i in range(0, len(names), size):
            out.append((org, names[i : i + size]))
    return out


async def _erase(cands: list[BenchGcCandidate], cfg: GcConfig, result: GcResult) -> None:
    from server.erasure.app.namespace_purge import erase_namespaces

    for org, names in _batches(cands, cfg.batch)[: cfg.sagas_per_pass]:
        out = await erase_namespaces(
            org, names, requester=cfg.requester, purge=cfg.purge, external=cfg.external
        )
        result.sagas += 1
        if str(out.status) != "completed":
            result.stopped_on = f"{out.erasure_id}:{out.status}"
            log.warning(
                "namespace_gc: saga %s ended %s (residue=%s); stopping this pass",
                out.erasure_id,
                out.status,
                getattr(out, "residue_count", "?"),
            )
            return
        result.erased += len(names)
