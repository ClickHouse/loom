"""Symbol fan-in on code.* results.

`loom.code_symbol_stats` holds, per called symbol, the number of live call
sites reaching it with symbols outside the repo dropped and call sites under
test/example/fuzzer/vendor paths counted separately (fan_in_nontest). The
weekly graph-stats pass writes it; this module is every read of it:

- `annotate_units` sets CodeUnit.fan_in from one point read on the table's
  ORDER BY — chunk units through their `symbol_ids`, symbol units through
  their own id. The value is `fan_in_name`: the most-called id sharing the
  unit's qualified name, because a name resolved by both tiers, overloaded,
  or instantiated per template has one row per id and a chunk carries only
  the id it was indexed with.
- `top_under` answers "which symbols under this prefix does the rest of the
  repo call most" for code.map and for code.search `sort_by='fan_in'`.
- `prior_factor` is the optional, capped rank prior for code.search; the top
  fan-in decile is mostly LOG_* macro expansions and exception constructors,
  so it defaults to cap 0.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from server.code.domain.units import CodeUnit, unit_from_symbol

logger = logging.getLogger("loom.code_fan_in")


async def stats_for(
    repo: Any, org: str, ns: str, symbol_ids: list[str]
) -> dict[str, dict[str, Any]]:
    ids = [s for s in dict.fromkeys(symbol_ids) if s]
    if not ids:
        return {}
    rows: dict[str, dict[str, Any]] = await repo.symbol_stats(org, ns, ids)
    return rows


async def top_under(
    repo: Any, org: str, ns: str, path_prefix: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Most-called symbols defined under `path_prefix`, tests excluded, one
    per qualified name, by non-test call sites."""
    top: list[dict[str, Any]] = await repo.top_symbol_stats(
        org, ns, path_prefix=path_prefix, limit=limit
    )
    return top


async def annotate_units(
    repo: Any,
    org: str,
    ns: str,
    units: list[CodeUnit],
    symbol_ids_of: dict[str, list[str]] | None = None,
    stats: dict[str, dict[str, Any]] | None = None,
) -> int:
    """Set `fan_in` (non-test call sites) on each unit; returns how many units received a non-zero
    value. A chunk unit maps through the symbols it defines (`symbol_ids_of[unit_id]`, the chunk
    row's symbol_ids; a function chunk carries exactly one); a symbol unit through its own id. A
    chunk defining several symbols (a class skeleton) reads the row whose qualified name is the
    chunk's own scope; only when no row names the scope does it fall back to the most-called member
    — measured: the skeleton of `IQueryTreeNode` otherwise wore `getNodeType`'s 239 and displaced
    that method from a hub page. Fails open. `stats` hands in rows a caller already read for the
    same ids (code.symbol's uses leg) so the table is read once per call."""
    if not units:
        return 0
    wanted: list[str] = []
    per_unit: dict[str, list[str]] = {}
    for u in units:
        ids = list((symbol_ids_of or {}).get(u.unit_id) or []) or [u.unit_id]
        per_unit[u.unit_id] = ids
        wanted.extend(ids)
    if stats is None:
        try:
            stats = await stats_for(repo, org, ns, wanted[:1000])
        except Exception as e:  # noqa: BLE001 — the field is additive
            logger.debug("fan-in annotation skipped: %s", e)
            return 0
    n = 0
    for u in units:
        ids = per_unit[u.unit_id]
        rows = [stats[s] for s in ids if s in stats]
        own = [r for r in rows if u.symbol and str(r.get("qualified_name") or "") == u.symbol]
        if own or len(ids) <= 1 or not u.symbol:
            pick = own if own else rows
            u.fan_in = max(
                (int(r.get("fan_in_name") or r["fan_in_nontest"]) for r in pick), default=0
            )
        else:
            # a chunk defining several symbols, none of them its own scope: the
            # scope (a class) has no callers of its own; a member's count is
            # not it, however many members have callers
            u.fan_in = 0
        n += int(u.fan_in > 0)
    return n


def prior_factor(fan_in: int, max_fan_in: int, cap: float) -> float:
    """1 + cap * log1p(fan_in) / log1p(max_fan_in): the pool's most-called
    unit gets the whole cap, a unit with one caller a sliver, nothing ever
    more than 1 + cap. cap <= 0 or an empty pool = 1."""
    if cap <= 0 or fan_in <= 0 or max_fan_in <= 0:
        return 1.0
    return 1.0 + cap * math.log1p(fan_in) / math.log1p(max_fan_in)


def order_by_fan_in(units: list[CodeUnit]) -> list[CodeUnit]:
    """Most-depended-on first; the retrieval score breaks ties (stable)."""
    return sorted(units, key=lambda u: (-u.fan_in, -u.score))


async def hub_units(
    repo: Any, org: str, ns: str, path_prefix: str, *, limit: int
) -> list[CodeUnit]:
    """The prefix's most-called symbols as bodyless symbol units, fan_in
    set, score 0 (they were not retrieved for the query)."""
    hubs = await top_under(repo, org, ns, path_prefix, limit=limit)
    if not hubs:
        return []
    rows = await repo.symbols_by_ids(org, ns, [str(h["symbol_id"]) for h in hubs])
    by_id = {str(r.get("symbol_id") or ""): r for r in rows}
    out: list[CodeUnit] = []
    for h in hubs:
        sid = str(h["symbol_id"])
        row = by_id.get(sid) or {
            "symbol_id": sid,
            "qualified_name": h["qualified_name"],
            "path": h["path"],
        }
        u = unit_from_symbol(row)
        u.fan_in = int(h.get("fan_in_name") or h["fan_in_nontest"])
        out.append(u)
    return out


def merge_hubs(ranked: list[CodeUnit], hubs: list[CodeUnit]) -> tuple[list[CodeUnit], int]:
    """Add hub units the retrieved pool does not already name (by unit id or
    qualified symbol); a retrieved unit for the same symbol keeps its body
    and takes the larger of its own and the hub's fan_in. Returns
    (units, added)."""
    if not hubs:
        return ranked, 0
    by_symbol = {u.symbol: u for u in ranked if u.symbol}
    ids = {u.unit_id for u in ranked}
    out = list(ranked)
    added = 0
    for h in hubs:
        if h.unit_id in ids:
            continue
        have = by_symbol.get(h.symbol)
        if have is not None:
            have.fan_in = max(have.fan_in, h.fan_in)
            continue
        out.append(h)
        added += 1
    return out, added
