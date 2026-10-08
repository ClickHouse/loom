"""The edge walks under callers, callees and impact: the edge kinds followed, the
one-hop walk, the multi-hop impact expansion, and the PR overlay rows merged into them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from server.code.api.models_symbol import CodeCallersInput, CodeEdgeWire
from server.code.app.spans import code_span, span_attrs
from server.code.store.index import CodeRepo
from server.code.store.index import prefer_scip_edges as _prefer_scip_edges
from server.core.errors import ValidationError

if TYPE_CHECKING:
    from server.code.app.index.pr_overlay import PrOverlay

logger = logging.getLogger(__name__)


# The edge kinds the callers / callees walks follow: calls and the class relations, plus a
# SQL statement's table reads and writes, so `code.callers` on a table answers "who reads /
# writes it".
_GRAPH_EDGE_KINDS = ["calls", "inherits", "overrides", "fixture_use", "reads", "writes"]


def _walk_kinds(input: CodeCallersInput) -> list[str]:
    """The edge kinds a callers / callees walk follows: every graph kind unless the
    request names a subset. A name outside the graph's kinds is a 400, not a silent empty
    answer."""
    wanted = list(input.edge_kinds or [])[:8]
    if not wanted:
        return _GRAPH_EDGE_KINDS
    kinds = [k for k in wanted if k in _GRAPH_EDGE_KINDS]
    if not kinds:
        raise ValidationError(
            f"edge_kinds must name one of {', '.join(_GRAPH_EDGE_KINDS)}; got {wanted!r}"
        )
    return kinds


async def _walk_edges(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeCallersInput,
    *,
    direction: str,
    seed_ids: list[str],
    via_for: dict[str, str],
    overlay: PrOverlay | None,
    via_member: dict[str, str],  # member id -> owner qualified name
) -> tuple[list[CodeEdgeWire], bool, int, dict[int, int]]:
    """The bounded hop walk of code.callers / code.callees: (edges, truncated, hops,
    distinct next-frontier size per hop). An edge whose near end is a dispatch seed
    carries that seed's qualified name as `via_override`; `max_nodes` bounds the union."""
    from server.code.domain.extract.registration import is_registration_caller

    edges_out: list[CodeEdgeWire] = []
    frontier = seed_ids
    seen_edges: set[tuple[str, str, str, str, int]] = set()
    seen_nodes: set[str] = set(seed_ids)
    truncated = False
    hops = 0
    by_hop_counts: dict[int, int] = {}
    kinds = _walk_kinds(input)
    read_limit = input.max_nodes * 2
    for _hop in range(max(1, min(input.depth, 3))):
        if not frontier:
            break
        if len(edges_out) >= input.max_nodes:
            truncated = True  # a frontier is left unwalked
            break
        hops += 1
        with code_span(f"{direction}.hop", depth=_hop + 1, seeds=len(frontier)) as sp:
            rows = await repo.edges_for_symbols(
                org,
                ns,
                frontier,
                direction=direction,
                edge_kinds=kinds,
                limit=read_limit,
                min_confidence=input.min_confidence,
            )
            if len(rows) >= read_limit:
                truncated = True  # the hop's read was cut at its limit
            rows = _prefer_scip_edges(rows)
            rows = _with_overlay_rows(rows, overlay, frontier, direction=direction)
            span_attrs(sp, edges=len(rows))
        next_frontier: list[str] = []
        for r in rows:
            key = (
                str(r["edge_kind"]),
                str(r["from_symbol_id"]),
                str(r["to_symbol_id"]),
                str(r["path"]),
                int(r["line"]),
            )
            if key in seen_edges:
                continue
            seen_edges.add(key)
            if len(edges_out) >= input.max_nodes:
                truncated = True
                break
            near = str(r["to_symbol_id"]) if direction == "callers" else str(r["from_symbol_id"])
            owner = via_member.get(near, "")
            if owner and str(r["from_qualified_name"]).startswith(owner + "::"):
                continue  # a member calling a sibling is not a caller of the class
            edges_out.append(
                CodeEdgeWire(
                    edge_kind=str(r["edge_kind"]),
                    from_symbol=str(r["from_qualified_name"]),
                    to_symbol=str(r["to_qualified_name"]),
                    from_symbol_id=str(r["from_symbol_id"]),
                    to_symbol_id=str(r["to_symbol_id"]),
                    path=str(r["path"]),
                    line=int(r["line"]),
                    tier=str(r["tier"]),
                    confidence=float(r["confidence"]),
                    via_override=via_for.get(near, ""),
                    # the caller is a registration function: the class is reached
                    # through a registry, not a call site
                    via="registration"
                    if is_registration_caller(str(r["from_qualified_name"]))
                    else ("member" if owner else ""),
                )
            )
            nxt = str(r["from_symbol_id"]) if direction == "callers" else str(r["to_symbol_id"])
            if nxt not in seen_nodes:
                seen_nodes.add(nxt)
                next_frontier.append(nxt)
        by_hop_counts[_hop + 1] = len(next_frontier)
        frontier = next_frontier
    return edges_out, truncated, hops, by_hop_counts


def _hop_counts(impacted: dict[str, dict[str, Any]]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for v in impacted.values():
        h = int(v.get("hop") or 0)
        counts[h] = counts.get(h, 0) + 1
    return dict(sorted(counts.items()))


_IMPACT_READ_LIMIT = 300


async def _expand_impact(
    repo: CodeRepo,
    org: str,
    ns: str,
    seed_ids: list[str],
    depth: int,
    *,
    read_limit: int,
    overlay: PrOverlay | None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Reverse edges (callers/inherits/overrides), depth <= 2, scip preferred.
    Returns (impacted, capped): capped is True when a hop's edge read hit `read_limit`,
    so the per-hop counts are themselves floors. `overlay` is a PR's post-image, whose
    edges are merged over each hop's rows: a call site the PR adds counts once, as tier
    `overlay`."""
    from server.code.domain.extract.registration import is_registration_caller

    impacted: dict[str, dict[str, Any]] = {}
    frontier = seed_ids
    capped = False
    for _hop in range(max(1, min(depth, 2))):
        if not frontier:
            break
        rows = await repo.edges_for_symbols(
            org,
            ns,
            frontier,
            direction="callers",
            edge_kinds=_GRAPH_EDGE_KINDS,
            limit=read_limit,
        )
        capped = capped or len(rows) >= read_limit
        rows = _prefer_scip_edges(rows)
        rows = _with_overlay_rows(rows, overlay, frontier, direction="callers")
        frontier = []
        for r in rows:
            sid = str(r["from_symbol_id"])
            if sid not in impacted and sid not in seed_ids:
                impacted[sid] = {
                    "qualified_name": str(r["from_qualified_name"]),
                    "path": str(r["path"]),
                    "hop": _hop + 1,
                    "tier": str(r["tier"]),
                }
                # A registration function reached the seed through a registry; the key
                # rides only when it applies.
                if is_registration_caller(str(r["from_qualified_name"])):
                    impacted[sid]["via"] = "registration"
                frontier.append(sid)
    return impacted, capped


def _with_overlay_rows(
    rows: list[dict[str, Any]],
    overlay: PrOverlay | None,
    frontier: list[str],
    *,
    direction: str,
) -> list[dict[str, Any]]:
    """The index's rows plus the PR post-image's edges at this frontier, deduplicated on
    (from, to, path); the index's tier wins."""
    if overlay is None or not overlay.ok:
        return rows
    from server.code.app.index.pr_overlay import merge_edge_rows

    return merge_edge_rows(rows, overlay.edge_rows(list(frontier), direction=direction))


def _overlay_added_rows(overlay: PrOverlay | None) -> list[dict[str, Any]]:
    """The functions a PR adds, as touched rows (`via: "added"`, tier `overlay`): the
    seeds the index cannot supply."""
    if overlay is None or not overlay.ok:
        return []
    return [{**s.row(), "via": "added"} for s in overlay.added]


async def _pr_overlay(repo: CodeRepo, org: str, ns: str, pr_number: int) -> Any:
    """The PR's overlay, or one whose `reason` says why it is off; a failed load is
    `overlay_note: error` and the read continues on the index alone."""

    from server.code.app.index import pr_overlay as po

    try:
        return await po.overlay_for_namespace(repo, org, ns, pr_number)
    except Exception as e:  # noqa: BLE001 — the overlay is additive; the index answers
        logger.warning("code overlay for PR %s failed: %s", pr_number, e)
        return po.PrOverlay(pr_number=int(pr_number), reason="error")
