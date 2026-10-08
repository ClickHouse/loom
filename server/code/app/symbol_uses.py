"""A definition brings its uses: the compact `uses` block a
single-definition `code.symbol` answer carries by default.

An agent that gets a single definition back often greps for its callers next, and rarely
asks for `uses: true` (the full grep-shaped list); the follow-up grep is rarely the name
itself, so the block is kept cheap and small rather than complete: the definition read carries what
the graph already knows about who calls it, in the shape the graph has — the live `calls`
edges INTO the definition (compiler tier first, the parser tier's confidence graded) plus the
declined sites named like it that the resolver could not bind (the candidate filters: same
language, written scope, not already an edge, at or above the deployment's confidence
floor). Two keyed reads — `to_symbol_id` on `code_edges`, the callee-name key on
`code_call_sites_unresolved` — each bounded by READ_CAP rows.

The block is small by construction: `count`, distinct `files`, the top
TOP_N sites (`path`, `line`, `caller`, `tier`, `confidence`), `truncated`,
`epistemic`. A hub — fan-in at or above the setting in `code_symbol_stats`,
or either read hitting its cap — is not listed: `{count_floor, skipped:
"hub"}` says how much would have come back, and `code.callers` is the op for
that. The block is budgeted BEFORE the bodies: `uses_tokens` is what it
cost, taken off `token_budget` before the definitions are trimmed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from server.code.app.candidates import (
    _seed_langs,
    _seed_qualified,
    _written_scope_matches,
    bare_names,
    confidence_of,
)
from server.code.domain.extract.treesitter import lang_for_path
from server.code.domain.index.epistemic import scip_coverage_from_rows
from server.code.store.index import SYMBOL_NOISE_PATH_RE, prefer_scip_edges
from server.settings import settings

logger = logging.getLogger(__name__)

TOP_N = 8
# `top` spends at most this many sites on one file, so eight sites name
# several callers rather than one file's five consecutive lines
PER_FILE = 2
# rows per keyed read; a read that fills it is a hub and the block says so
READ_CAP = 500
TIER_RANK = {"scip": 0, "treesitter": 1, "candidate": 2, "mention": 3}
_NOISE = re.compile(SYMBOL_NOISE_PATH_RE)
# A type has no `calls` edges into it: its uses are the lines that name it,
# read the way the `uses: true` list is (the chunk index, definition spans dropped)
# and served as `tier: mention` — a lower bound by construction.
TYPE_KINDS = frozenset({"class", "struct", "enum", "union", "typedef", "alias", "concept", "type"})
MENTION_CONFIDENCE = {"code": 1.0, "comment": 0.3, "include": 0.3}
# the mention read skips the directories the symbol ranking demotes (a
# scope takes at most eight globs)
MENTION_EXCLUDE_GLOBS = (
    "tests/**",
    "**/tests/**",
    "**/examples/**",
    "**/fuzzers/**",
    "**/benchmarks/**",
    "ci/**",
    "contrib/**",
    "utils/**",
)


def single_declaration(rows: list[dict[str, Any]]) -> bool:
    """Exactly one definition, or one row per tier for the same declaration
    (the compiler's header line and the parser's body span). Overloads,
    template instantiations and same-named classes are several."""
    names = {str(r.get("qualified_name") or "") for r in rows}
    if len(names) != 1:
        return False
    tiers = [str(r.get("tier") or "") for r in rows]
    return len(tiers) == len(set(tiers))


def hub_fan_in(stats: dict[str, dict[str, Any]], symbol_ids: list[str]) -> int:
    """The largest fan-in the stats table knows for these ids (0 when it has
    none): `fan_in_name` covers the ids that share the qualified name."""
    best = 0
    for sid in symbol_ids:
        row = stats.get(sid)
        if row:
            best = max(best, int(row.get("fan_in_name") or row.get("fan_in_calls") or 0))
    return best


def _site(path: str, line: int, caller: str, tier: str, confidence: float) -> dict[str, Any]:
    return {
        "path": path,
        "line": int(line),
        "caller": caller,
        "tier": tier,
        "confidence": round(float(confidence), 3),
    }


def _order(s: dict[str, Any]) -> tuple[int, int, float, str, int]:
    return (
        TIER_RANK.get(str(s["tier"]), 3),
        1 if _NOISE.search(str(s["path"])) else 0,
        -float(s["confidence"]),
        str(s["path"]),
        int(s["line"]),
    )


def _candidate_sites(
    rows: list[dict[str, Any]],
    seeds: list[dict[str, Any]],
    edge_sites: set[tuple[str, int]],
    *,
    floor: float,
) -> tuple[list[dict[str, Any]], int]:
    """(sites at or above the floor, declined rows that passed the language
    and scope filters) — the second number is what makes the answer a lower
    bound even when nothing is listed."""
    langs = _seed_langs(seeds)
    qualified = _seed_qualified(seeds)
    out: list[dict[str, Any]] = []
    declined = 0
    for r in rows:
        path = str(r.get("path") or "")
        if langs and lang_for_path(path) not in langs:
            continue
        if not _written_scope_matches(str(r.get("call_text") or ""), qualified):
            continue
        if (path, int(r.get("line") or 0)) in edge_sites:
            continue
        declined += 1
        conf = confidence_of(str(r.get("disposition") or ""), int(r.get("candidates") or 0))
        if conf < floor:
            continue
        out.append(
            _site(
                path,
                int(r.get("line") or 0),
                str(r.get("from_qualified_name") or ""),
                "candidate",
                conf,
            )
        )
    return out, declined


def is_type(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(str(r.get("kind") or "").lower() in TYPE_KINDS for r in rows)


def mention_block(lines: list[dict[str, Any]], *, truncated: bool) -> dict[str, Any]:
    """The compact block from the `uses: true` grep-shaped lines: a mention's `caller`
    is the enclosing scope, its confidence says whether the line is code."""
    sites = [
        _site(
            str(u["path"]),
            int(u["line"]),
            str(u.get("scope") or ""),
            "mention",
            MENTION_CONFIDENCE.get(str(u.get("kind") or "code"), 1.0),
        )
        for u in lines
    ]
    sites.sort(key=_order)
    top = top_sites(sites)
    return {
        "count": len(sites),
        "files": len({s["path"] for s in sites}),
        "top": top,
        "truncated": truncated or len(sites) > len(top),
        "epistemic": "lower_bound",
    }


def top_sites(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first TOP_N sites in order, at most PER_FILE from any one path.
    `count` and `files` carry the totals; `top` is where to look first, so
    a file's third site is left out even when that leaves the list short."""
    per_path: dict[str, int] = {}
    picked: list[dict[str, Any]] = []
    for s in sites:
        if per_path.get(s["path"], 0) >= PER_FILE:
            continue
        per_path[s["path"]] = per_path.get(s["path"], 0) + 1
        picked.append(s)
        if len(picked) >= TOP_N:
            break
    return picked


def hub_block(count_floor: int) -> dict[str, Any]:
    return {"count_floor": int(count_floor), "skipped": "hub", "epistemic": "lower_bound"}


def _count_floor(
    edges: list[dict[str, Any]], misses: list[dict[str, Any]], seeds: list[dict[str, Any]]
) -> int:
    """The sites a capped read proves: a call site both tiers indexed counts once, and a
    declined row counts only when it passes the language, scope and already-an-edge
    filters the listed block applies — a floor may never exceed the true count."""
    live = prefer_scip_edges(edges)
    edge_sites = {(str(e["path"]), int(e["line"])) for e in live}
    _sites, declined = _candidate_sites(misses, seeds, edge_sites, floor=1.1)
    return len(live) + declined


def build_block(
    edges: list[dict[str, Any]],
    misses: list[dict[str, Any]],
    seeds: list[dict[str, Any]],
    *,
    floor: float,
) -> dict[str, Any]:
    """The compact block from the two reads' rows. Pure; the tests hold it
    to a stub repo."""
    live = prefer_scip_edges(edges)
    edge_sites = {(str(e["path"]), int(e["line"])) for e in live}
    sites = [
        _site(
            str(e["path"]),
            int(e["line"]),
            str(e.get("from_qualified_name") or ""),
            str(e.get("tier") or ""),
            float(e.get("confidence") or 0.0),
        )
        for e in live
    ]
    candidates, declined = _candidate_sites(misses, seeds, edge_sites, floor=floor)
    sites.extend(candidates)
    sites.sort(key=_order)
    top = top_sites(sites)
    # Exact only when the compiler tier indexed EVERY seed's file (one scip row in a
    # file proves it, as for the graph ops); a seed only the parser saw can have callers
    # the compiler tier would bind that no edge here names.
    seed_paths = {str(s.get("path") or "") for s in seeds}
    compiler_seed = (
        bool(seeds) and "" not in seed_paths and seed_paths <= scip_coverage_from_rows(seeds)
    )
    return {
        "count": len(sites),
        "files": len({s["path"] for s in sites}),
        "top": top,
        "truncated": len(sites) > len(top),
        "epistemic": "exact" if compiler_seed and declined == 0 else "lower_bound",
    }


MentionLeg = Callable[
    [Any, str, str, list[dict[str, Any]]], Awaitable[tuple[list[dict[str, Any]], bool]]
]


async def uses_brief(
    repo: Any,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
    *,
    mention_leg: MentionLeg | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The leg: fan-in gate from the stats table, then the two keyed reads
    concurrently — or, for a type, the mention read (`mention_leg`, the `uses: true`
    grep, handed in by the handler). Returns (block, stats) — the stats are
    handed on so the definitions' `fan_in` field does not read the table
    twice. Never raises: a failed read yields an empty lower bound."""
    from server.code.app import fan_in as code_fan_in

    ids = [str(r.get("symbol_id") or "") for r in rows if r.get("symbol_id")]
    stats: dict[str, dict[str, Any]] = {}
    try:
        stats = await code_fan_in.stats_for(repo, org, ns, ids)
    except Exception as e:  # noqa: BLE001 — the gate falls back to the read cap
        logger.debug("code.symbol uses fan-in gate skipped: %s", e)
    threshold = int(settings.code_symbol_uses_hub_fan_in)
    fan_in = hub_fan_in(stats, ids)
    if threshold and fan_in >= threshold:
        return hub_block(fan_in), stats
    if mention_leg is not None and is_type(rows):
        try:
            lines, truncated = await mention_leg(repo, org, ns, rows)
        except Exception as e:  # noqa: BLE001 — additive, never fatal
            logger.debug("code.symbol uses mention leg failed: %s", e)
            lines, truncated = [], False
        return mention_block(lines, truncated=truncated), stats
    names = bare_names(rows)
    try:
        edges, misses = await asyncio.gather(
            repo.edges_for_symbols(org, ns, ids, direction="callers", limit=READ_CAP),
            repo.live_call_misses_for_names(org, ns, names, limit=READ_CAP) if names else _none(),
        )
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        logger.debug("code.symbol uses leg failed: %s", e)
        return {
            "count": 0,
            "files": 0,
            "top": [],
            "truncated": False,
            "epistemic": "lower_bound",
            "note": "uses read failed",
        }, stats
    if len(edges) >= READ_CAP or len(misses) >= READ_CAP:
        return hub_block(_count_floor(edges, misses, rows)), stats
    floor = float(settings.code_callers_candidate_min_confidence)
    return build_block(edges, misses, rows, floor=floor), stats


async def _none() -> list[dict[str, Any]]:
    return []
