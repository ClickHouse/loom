"""What the index can vouch for under a structural answer: scip coverage and
staleness of the seed paths, declined call sites, unindexed paths, and whether
constructor call edges were ingested (process cache).
"""

from __future__ import annotations

import logging
from typing import Any

from server.code.app.symbol.dispatch import PROBE_ERROR_TTL_S
from server.code.app.ttl_cache import TtlCache
from server.code.domain.extract.edges_treesitter import is_constructor_name
from server.code.domain.extract.treesitter import lang_for_path
from server.code.domain.index.compiler_tiers import state_value
from server.code.domain.index.epistemic import Envelope, scip_coverage_from_rows
from server.code.domain.index.verdicts import GRAPHED_LANGS as _GRAPHED_LANGS
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


async def _declined_safe(
    repo: CodeRepo, org: str, ns: str, seeds: list[dict[str, Any]]
) -> tuple[int, int]:
    """_declined_calls_for, fail-open: a disclosure lookup never fails the answer."""
    try:
        return await _declined_calls_for(repo, org, ns, seeds)
    except Exception as e:  # noqa: BLE001
        logger.debug("declined-call lookup skipped: %s", e)
        return 0, 0


async def _declined_outgoing_safe(
    repo: CodeRepo, org: str, ns: str, seeds: list[dict[str, Any]]
) -> tuple[int, int]:
    """(unresolved, ambiguous) declined call sites INSIDE the seeds — what a callees walk
    cannot see the target of — from the seed paths' live declined rows. Fail-open like
    `_declined_safe`: a disclosure lookup never fails the answer."""
    from server.code.app.candidates import _inside_seed

    paths = list(dict.fromkeys(str(s.get("path") or "") for s in seeds if s.get("path")))
    if not paths:
        return 0, 0
    try:
        by_path = await repo.live_call_misses_for_paths(org, ns, paths)
    except Exception as e:  # noqa: BLE001 — a disclosure read; the walk stands
        logger.debug("outgoing declined-call lookup skipped: %s", e)
        return 0, 0
    unresolved = ambiguous = 0
    for path, rows in by_path.items():
        for r in rows:
            if not _inside_seed(r, seeds, path):
                continue
            if str(r.get("disposition") or "") == "unresolved":
                unresolved += 1
            else:
                ambiguous += 1
    return unresolved, ambiguous


async def _scip_coverage(
    repo: CodeRepo, org: str, ns: str, paths: list[str], rows: list[dict[str, Any]]
) -> set[str]:
    """Seed paths the compiler tier covers. Proven first from symbol rows
    already in hand (a scip row in the path), the remainder read once from
    the live symbol table. Fail-open to UNPROVEN: a failed read can only
    make the answer a lower bound, never claim exact."""
    covered = scip_coverage_from_rows(rows)
    remaining = [p for p in dict.fromkeys(paths) if p and p not in covered]
    if remaining:
        try:
            covered |= await repo.scip_covered_paths(org, ns, remaining)
        except Exception as e:  # noqa: BLE001
            logger.debug("scip coverage read skipped: %s", e)
    return covered


async def _scip_stale(repo: CodeRepo, org: str, ns: str, edges: list[Any]) -> set[str]:
    """Served compiler-tier edge paths the compiler tier has not seen since they
    changed. Fail-open to UNPROVEN: a failed read counts every one of them stale, so
    the answer can only fall to a lower bound, never claim exact."""
    paths = list(dict.fromkeys(e.path for e in edges if e.tier == "scip"))
    if not paths:
        return set()
    try:
        return await repo.scip_stale_paths(org, ns, paths)
    except Exception as e:  # noqa: BLE001 — a freshness read must not fail the walk
        logger.debug("scip staleness read skipped: %s", e)
        return set(paths)


def _graph_paths(paths: list[str], rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(paths with a symbol row, graphed-language paths without one). Test
    files are leaves of the call graph and a .md or .sql defines nothing
    callable: neither is a seed of the verdict, whatever the walk seeds."""
    from server.code.domain.index.test_gate import is_test_path

    with_rows = {str(r.get("path") or "") for r in rows}
    known: list[str] = []
    bare: list[str] = []
    for p in dict.fromkeys(paths):
        if not p or is_test_path(p):
            continue
        if p in with_rows:
            known.append(p)
        elif lang_for_path(p) in _GRAPHED_LANGS:
            bare.append(p)
    return known, bare


async def _unindexed_paths(repo: CodeRepo, org: str, ns: str, bare: list[str]) -> set[str]:
    """Which symbol-less graphed-language seed paths the index does not hold
    at all — their callers are external to the graph. One held but without
    symbols (a header of constants) defines nothing to call and is no
    cause. Fail-open to 'unindexed': a failed read may only lower the
    verdict, never raise it."""
    if not bare:
        return set()
    try:
        return set(bare) - await repo.live_paths_present(org, ns, bare)
    except Exception as e:  # noqa: BLE001
        logger.debug("indexed-paths read skipped: %s", e)
        return set(bare)


def _env_fields(env: Envelope) -> dict[str, Any]:
    return {"epistemic": env.epistemic, "causes": env.causes}


async def _declined_calls_for(
    repo: CodeRepo, org: str, ns: str, seeds: list[dict[str, Any]]
) -> tuple[int, int]:
    """(unresolved, ambiguous) declined call sites named like any seed."""
    from server.code.app.candidates import bare_names

    bare = bare_names(seeds)
    if not bare:
        return 0, 0
    counts = await repo.call_misses_for_names(org, ns, bare)
    unresolved = sum(int(c.get("unresolved", 0)) for c in counts.values())
    ambiguous = sum(int(c.get("ambiguous", 0)) for c in counts.values())
    return unresolved, ambiguous


_CTOR_CACHE: TtlCache[tuple[str, str], tuple[bool, bool]] = TtlCache(3600.0)


async def _ctor_edge_coverage(repo: CodeRepo, org: str, ns: str) -> tuple[bool, bool]:
    """(compiler tier, parser tier): does each carry constructor calls through factories?
    One code_index_state read, cached 1 h per namespace like the `overrides` probe. The
    compiler tier says so when a full index converted with its source was applied
    (`scip_ctor_calls`); the parser tier when the namespace's edge stage has converged on
    the running extractor stamp (or was indexed by it). Fail closed to (False, False): an
    error can only lower an answer, and that answer is kept only PROBE_ERROR_TTL_S."""
    hit = _CTOR_CACHE.get((org, ns))
    if hit is not None:
        return hit
    ttl: float | None = None
    try:
        from server.code.app.index.scip_delta import STATE_SCIP_CTOR_CALLS
        from server.code.domain.extract.extractor_stamp import stage_stamps

        state = await repo.all_state(org, ns)
        scip_ok = state_value(state, STATE_SCIP_CTOR_CALLS) == "1"
        edge_stamp = state_value(state, "extractor_stamp_edge")
        ts_ok = bool(edge_stamp) and edge_stamp == stage_stamps()["edge"]
        cov = (scip_ok, ts_ok)
    except Exception:  # noqa: BLE001 — fail closed: an error can only lower an answer
        cov, ttl = (False, False), PROBE_ERROR_TTL_S
    _CTOR_CACHE.put((org, ns), cov, ttl_s=ttl)
    return cov


def _has_ctor_seed(rows: list[dict[str, Any]]) -> bool:
    return any(is_constructor_name(str(r.get("qualified_name") or "")) for r in rows)


async def _ctor_edges_absent(
    repo: CodeRepo, org: str, ns: str, seed_paths: list[str], scip_paths: set[str]
) -> bool:
    """For the tier that answers for these seed paths (the compiler tier where it covers
    them, the parser tier otherwise), are constructor calls through factories missing
    from the index? Mixed coverage needs both."""
    scip_ok, ts_ok = await _ctor_edge_coverage(repo, org, ns)
    covered = [p in scip_paths for p in seed_paths if p]
    if covered and all(covered):
        return not scip_ok
    if covered and not any(covered):
        return not ts_ok
    return not (scip_ok and ts_ok)


_CTOR_NOTE = (
    "seed is a constructor and the index answering for it predates constructor calls "
    "through factories (std::make_shared<T>, std::make_unique<T>): "
    "the class may be constructed where no edge says so — grep the class name"
)
