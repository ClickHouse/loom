"""A type's supertypes and subtypes from `inherits` edges, for code.symbol."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.code.app.symbol.resolve import _seed_ids_of
from server.code.store.index import CodeRepo
from server.code.store.index import prefer_scip_edges as _prefer_scip_edges

logger = logging.getLogger(__name__)


_HIERARCHY_KINDS = frozenset({"class", "struct", "interface", "enum"})


_HIERARCHY_LIMIT = 60


def _heritage_via(derived_kind: str, base_kind: str) -> str:
    """`implements` when a class takes an interface, `extends` otherwise —
    the TypeScript handbook's distinction (an implements clause is only a
    check; extends gives the members). The index stores one `inherits`
    kind for both, as SCIP does; the kinds of the two ends tell them apart."""
    return "implements" if base_kind == "interface" and derived_kind != "interface" else "extends"


def _hierarchy_rows(
    edges: list[dict[str, Any]],
    far: str,
    kinds: dict[str, str],
    near_kind: str,
) -> list[dict[str, Any]]:
    """One side of the hierarchy, folded by the far symbol's qualified name
    (both tiers carry the same type under different ids; compiler edges sort
    first, so the fold keeps them)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in edges:
        far_id = str(e[f"{far}_symbol_id"])
        symbol = str(e.get(f"{far}_qualified_name") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        far_kind = kinds.get(far_id, "")
        derived_kind, base_kind = (near_kind, far_kind) if far == "to" else (far_kind, near_kind)
        out.append(
            {
                "symbol": symbol,
                "kind": far_kind,
                "via": _heritage_via(derived_kind, base_kind),
                "tier": str(e.get("tier") or ""),
                "confidence": float(e.get("confidence") or 0.0),
                "path": str(e.get("path") or ""),
                "line": int(e.get("line") or 0),
            }
        )
    return out


async def _symbol_hierarchy(
    repo: CodeRepo,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """A type definition's `supertypes` (its inherits edges out) and `subtypes` (inherits
    edges in: what implements or extends it), both tiers, seeded from every tier's id of
    the definition. Two keyed edge reads plus one symbols read for the far kinds; nothing
    for functions."""
    seeds = [r for r in rows if str(r.get("kind") or "") in _HIERARCHY_KINDS]
    if not seeds:
        return {}
    try:
        return await _hierarchy_of(repo, org, ns, seeds)
    except Exception as e:  # noqa: BLE001 — a failed side read never fails the definition
        logger.warning("code.symbol hierarchy read failed: %s", e)
        return {}


async def _hierarchy_of(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    ids = _seed_ids_of(seeds)
    ups, downs = await asyncio.gather(
        repo.edges_for_symbols(
            org,
            ns,
            ids,
            direction="callees",
            edge_kinds=["inherits"],
            limit=_HIERARCHY_LIMIT,
        ),
        repo.edges_for_symbols(
            org,
            ns,
            ids,
            direction="callers",
            edge_kinds=["inherits"],
            limit=_HIERARCHY_LIMIT,
        ),
    )
    ups, downs = _prefer_scip_edges(ups), _prefer_scip_edges(downs)
    far_ids = list(
        dict.fromkeys(
            [*(str(e["to_symbol_id"]) for e in ups), *(str(e["from_symbol_id"]) for e in downs)]
        )
    )
    kinds = {
        str(s["symbol_id"]): str(s.get("kind") or "")
        for s in (await repo.symbols_by_ids(org, ns, far_ids) if far_ids else [])
    }
    seed_kind = str(seeds[0].get("kind") or "")
    out: dict[str, list[dict[str, Any]]] = {}
    if ups:
        out["supertypes"] = _hierarchy_rows(ups, "to", kinds, seed_kind)
    if downs:
        out["subtypes"] = _hierarchy_rows(downs, "from", kinds, seed_kind)
    return out
