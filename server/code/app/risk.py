"""Per-result risk annotations + one-hop callee context for code.search.

"Is it safe to change this?" is a question the index already holds every
ingredient for — call edges, PR history, the test map. Each returned unit
gains a small risk block:

  churn   merged PRs touching the unit's path in the trailing year. A FLOOR,
          not a total: bounded by how far code_pr_history has been backfilled.
  amp     change amplification — distinct 1-hop caller symbols across the
          unit's symbols (calls/inherits/overrides, scip-preferred). The
          symbols that feel an edit immediately; code.impact holds the
          transitive story.
  tested  a mapped test reaches this path (code_test_map, any method). Like
          the map itself this sees in-process linkage, not subprocess-level
          test harnesses — read False as "no mapped test", never "untested".
  callees a capped one-hop callee slice {name, path, line}, so the agent
          sees a hit's immediate neighborhood without a second code.callers
          round-trip.

Budget contract: the caller RESERVES headroom out of the request's
token_budget before retrieval fills bodies, then this module returns its
actual spend to be added to tokens_returned. If the annotations run over
the reservation, callees are dropped tail-rank-first until they fit — the
integer risk fields always survive (they cost ~a pointer each). The
request's budget ceiling is therefore never exceeded by annotation.

Failure contract: annotation is additive telemetry on a served result —
any exception here must degrade to un-annotated units, never fail the
search. The handler wraps the call accordingly; this module only promises
not to mutate units on a partial failure mid-way (it stages, then applies).

Placement contract. The five reads fall in two rounds: the PATH facts (churn per path,
the function symbols of each path) need only the candidate paths, and the UNIT facts
(mapped tests, callers, callees) need the served units' symbols, which come from the
path round. `RiskPrefetch.start()` runs the path round on the ranked pool's head (top_k
x 2 plus the history anchors) from inside `search()` once the in-memory priors have
settled the order, beside the outcome read, the diversity pass and the body budget that
follow; the handler's `annotate()` then runs the unit round for exactly the served units
and attaches. The output is byte-identical to the sequential read's: the unit round IS
the sequential read (same statements, same key sets), and the path round's rows are
per-path keyed reads with no cross-path limit in play, so the head's rows filtered to
the served paths are the served paths' own rows in the same order. A served unit whose
path the head did not carry (a diversity refill from a new file, a lifted anchor) has
its path read at the join first and is counted as `risk_annotate_missed`.

Precomputed rows. The block is read first from
`loom.code_unit_risk` (code/app/unit_risk.py): the prefetch reads the head's unit
ids in one keyed statement and runs the path round only for the head units without a
fresh row; the join attaches the rows for the served units they cover and the live reads
serve the rest. `RiskPrefetch.source` says which (table | live | mixed) and `.stale` how
many served units were read live because their row was missing or older than
`code_unit_risk_max_age_s`; the handler discloses both.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from server.core.background import create_deferred_task

if TYPE_CHECKING:
    from server.code.domain.units import CodeUnit
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_risk")

CHURN_WINDOW_DAYS = 365
CALLEES_PER_UNIT = 3
# Rows read per source symbol for the callee slice: CALLEES_PER_UNIT plus
# headroom for the two filters applied after the read (anonymous-namespace
# names dropped, parser rows dropped where the compiler saw the same call).
# Both tiers can contribute up to this many rows each, ordered compiler
# first, so the cap never removes a scip row in favour of a parser row.
CALLEES_PER_SOURCE_READ = CALLEES_PER_UNIT * 8
# Per-unit reservation estimate: 3 short ints + bool (~14 tok) plus
# CALLEES_PER_UNIT compact rows (~18 tok each). Used by the handler to
# carve the reserve BEFORE body-fill; actual spend is measured after.
RESERVE_PER_UNIT_TOKENS = 14 + CALLEES_PER_UNIT * 18


# Read limits of the annotation's statements. The symbol read floors its own limit at
# 300 rows per path asked, so a wider path set never starves a path the served set alone
# would have read in full. The tested read has no limit (a DISTINCT over the served
# paths' rows).
SYMBOLS_LIMIT = 400
CALLERS_LIMIT = 1000
CALLEES_LIMIT = 500
CALLER_KINDS = ["calls", "inherits", "overrides", "fixture_use"]
# Candidates per served slot whose paths the prefetch reads. The priors that still run after the
# hook (outcome boost, history lift, diversity, saturation) move units more than files: on 60
# replayed consumer shapes a head of 2 x top_k carried the served paths on 51 calls, 3 x on 56, 4 x
# on 58, 6 x on 60; the path round's cost grows with the distinct paths (the symbol read is a
# skip-index scan per path), so 4 x is the trade taken.
PREFETCH_PER_SLOT = 4


def reserve_for(top_k: int) -> int:
    return min(600, top_k * RESERVE_PER_UNIT_TOKENS)


def _overlaps(unit: CodeUnit, sym: dict[str, Any]) -> bool:
    if str(sym.get("path") or "") != unit.path:
        return False
    s, e = int(sym.get("start_line") or 0), int(sym.get("end_line") or 0)
    return s <= unit.end_line and e >= unit.start_line


def _tokens(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"))) // 4


@dataclass
class _PathFacts:
    """The path round: churn per path and the function symbols of each path
    (both tiers, scip preferred, the statement's order), for `paths`."""

    paths: set[str] = field(default_factory=set)
    churn: dict[str, int] = field(default_factory=dict)
    symbol_rows: list[dict[str, Any]] = field(default_factory=list)

    def merge(self, other: _PathFacts) -> None:
        self.paths |= other.paths
        self.churn.update(other.churn)
        self.symbol_rows += other.symbol_rows


def symbols_of(units: list[CodeUnit], symbol_rows: list[dict[str, Any]]) -> list[list[str]]:
    """Symbols per unit, matched by path + line overlap. Units minted straight
    from a symbol row (kind == "symbol") carry the symbol_id as unit_id."""
    out: list[list[str]] = []
    for u in units:
        if u.kind == "symbol" and u.unit_id:
            out.append([u.unit_id])
        else:
            out.append([str(s["symbol_id"]) for s in symbol_rows if _overlaps(u, s)])
    return out


async def _read_path_facts(repo: CodeRepo, org: str, ns: str, paths: list[str]) -> _PathFacts:
    """Churn and symbol rows for `paths`: two independent keyed reads awaited together
    (one round trip of wall, not two). The symbol read is the slow one either way —
    code_symbols sorts by (tier, qualified_name), so a path is a skip-index scan — and
    it costs the same whether it returns the file's every function or the few a span
    filter would keep (a span filter saves nothing and would break the two-tier merge)."""
    facts = _PathFacts(paths=set(paths))
    if not paths:
        return facts
    facts.churn, facts.symbol_rows = await asyncio.gather(
        repo.pr_churn_for(org, ns, paths, days=CHURN_WINDOW_DAYS),
        repo.functions_for_paths(org, ns, paths, limit=SYMBOLS_LIMIT),
    )
    return facts


async def _read_unit_facts(
    repo: CodeRepo, org: str, ns: str, paths: list[str], symbol_ids: list[str]
) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """The unit round: (tested paths, caller rows, callee rows) for the served paths and
    their symbols, three independent reads awaited together. `tested` is the set of
    served paths a mapped test reaches — a DISTINCT over the keyed rows, so a path with a
    few tests reads True even when a file with many more is served beside it. The two
    hops are reads of one table; the callee read is capped per source symbol in SQL so
    one hub cannot spend the flat limit on itself while the other units get nothing."""

    async def _none() -> list[dict[str, Any]]:
        return []

    async def _no_paths() -> set[str]:
        return set()

    tests, callers, callees = await asyncio.gather(
        repo.tested_paths(org, ns, paths) if paths else _no_paths(),
        repo.edges_for_symbols(
            org,
            ns,
            symbol_ids,
            direction="callers",
            edge_kinds=CALLER_KINDS,
            limit=CALLERS_LIMIT,
        )
        if symbol_ids
        else _none(),
        repo.edges_for_symbols(
            org,
            ns,
            symbol_ids,
            direction="callees",
            edge_kinds=["calls"],
            limit=CALLEES_LIMIT,
            per_match=CALLEES_PER_SOURCE_READ,
        )
        if symbol_ids
        else _none(),
    )
    return tests, callers, callees


Staged = tuple["CodeUnit", int, int, bool, list[dict[str, Any]]]


def stage_units(
    units: list[CodeUnit],
    *,
    churn: dict[str, int],
    symbol_rows: list[dict[str, Any]],
    tested_paths: set[str],
    caller_raw: list[dict[str, Any]],
    callee_raw: list[dict[str, Any]],
) -> list[Staged]:
    """The attach's arithmetic, pure: (unit, churn, amp, tested, callees) per unit from
    the path facts and the unit facts. The live read and the precomputed table both go
    through here, so a row equals what the reads would have attached."""
    from server.code.store.index import prefer_scip_edges

    unit_syms = symbols_of(units, symbol_rows)
    callers_by_target: dict[str, set[str]] = {}
    callees_by_source: dict[str, list[dict[str, Any]]] = {}
    for r in prefer_scip_edges(caller_raw):
        callers_by_target.setdefault(str(r["to_symbol_id"]), set()).add(
            str(r["from_qualified_name"])
        )
    for r in prefer_scip_edges(callee_raw):
        if "$anonymous" in str(r["to_qualified_name"]):
            continue  # mangled anon-namespace names are noise to an agent
        lst = callees_by_source.setdefault(str(r["from_symbol_id"]), [])
        if len(lst) < CALLEES_PER_UNIT and not any(
            c["name"] == str(r["to_qualified_name"]) for c in lst
        ):
            lst.append(
                {
                    "name": str(r["to_qualified_name"]),
                    "path": str(r["path"]),
                    "line": int(r["line"] or 0),
                }
            )

    staged: list[Staged] = []
    for i, u in enumerate(units):
        caller_names: set[str] = set()
        hop: list[dict[str, Any]] = []
        for sid in unit_syms[i]:
            caller_names |= callers_by_target.get(sid, set())
            for c in callees_by_source.get(sid, []):
                if len(hop) < CALLEES_PER_UNIT and not any(h["name"] == c["name"] for h in hop):
                    hop.append(c)
        staged.append(
            (u, int(churn.get(u.path, 0)), len(caller_names), u.path in tested_paths, hop)
        )
    return staged


def apply_staged(staged: list[Staged], *, reserve_tokens: int) -> int:
    """Fit callees into the reservation tail-rank-first, attach, return the
    tokens spent (<= reserve_tokens). The integer fields always survive."""

    def spend() -> int:
        return sum(_tokens([ch, amp, ts]) + _tokens(hop) for _, ch, amp, ts, hop in staged)

    for i in range(len(staged) - 1, -1, -1):
        if spend() <= reserve_tokens:
            break
        u, ch, amp, ts, _ = staged[i]
        staged[i] = (u, ch, amp, ts, [])

    cost = spend()
    if cost > reserve_tokens:  # ints alone exceed reserve — annotate anyway,
        cost = reserve_tokens  # spend is capped at what was carved out
    for u, ch, amp, ts, hop in staged:
        u.churn, u.amp, u.tested, u.callees = ch, amp, ts, hop
    return cost


async def _annotate_from(
    repo: CodeRepo,
    org: str,
    ns: str,
    units: list[CodeUnit],
    path_facts: _PathFacts,
    *,
    reserve_tokens: int,
    precomputed: dict[str, Any] | None = None,
) -> int:
    """The unit round for the `units` without a precomputed row (whose paths
    `path_facts` covers), then the attach over every unit in served order.
    Returns tokens spent (guaranteed <= reserve_tokens)."""
    pre = precomputed or {}
    live = [u for u in units if u.unit_id not in pre]
    staged_live: dict[int, Staged] = {}
    if live:
        paths = list({u.path for u in live if u.path})
        served = set(paths)
        symbol_rows = [s for s in path_facts.symbol_rows if str(s.get("path") or "") in served]
        all_ids = list({sid for ids in symbols_of(live, symbol_rows) for sid in ids})
        tested, caller_raw, callee_raw = await _read_unit_facts(repo, org, ns, paths, all_ids)
        rows = stage_units(
            live,
            churn=path_facts.churn,
            symbol_rows=symbol_rows,
            tested_paths=tested,
            caller_raw=caller_raw,
            callee_raw=callee_raw,
        )
        staged_live = {id(u): row for u, row in zip(live, rows, strict=True)}
    staged = [
        pre[u.unit_id].as_staged(u) if u.unit_id in pre else staged_live[id(u)] for u in units
    ]
    return apply_staged(staged, reserve_tokens=reserve_tokens)


async def annotate_units(
    repo: CodeRepo,
    org: str,
    ns: str,
    units: list[CodeUnit],
    *,
    reserve_tokens: int,
) -> int:
    """Fill churn/amp/tested/callees on units in place, reading for exactly these units: the
    precomputed rows first when the table is on, then the path round and the unit round for the
    units without a fresh row (two round trips of wall). Returns tokens spent (guaranteed <=
    reserve_tokens)."""
    from server.code.app import unit_risk as code_unit_risk

    if not units:
        return 0
    pre = await code_unit_risk.read_rows(repo, org, ns, [u.unit_id for u in units])
    paths = list({u.path for u in units if u.path and u.unit_id not in pre})
    facts = await _read_path_facts(repo, org, ns, paths)
    return await _annotate_from(
        repo, org, ns, units, facts, reserve_tokens=reserve_tokens, precomputed=pre
    )


@dataclass
class _Prefetched:
    """What the hook read for the head: fresh precomputed rows by unit id and
    the path facts of the head units without one."""

    rows: dict[str, Any] = field(default_factory=dict)
    facts: _PathFacts = field(default_factory=_PathFacts)
    asked: set[str] = field(default_factory=set)  # the head's unit ids the table was read for


class RiskPrefetch:
    """The annotation's path round, started on the ranked pool's head from inside
    search() and joined by the handler on the served units — and, with the table on, the
    keyed read of the head's precomputed rows that replaces the path round for the units
    it covers.

    `start()` is called once, with the ranked candidates, at the point where
    the in-memory priors have run and no code_span frame is open (the task
    copies the request's stage wall, so its span lands on `risk_prefetch`);
    it never raises. `annotate()` waits for the task, reads any served path
    the head did not carry, runs the unit round for the served units without
    a row and attaches; a prefetch that failed degrades to the sequential
    read, so the served annotation is the same either way."""

    def __init__(self, repo: CodeRepo, org: str, ns: str, *, top_k: int) -> None:
        self._repo, self._org, self._ns = repo, org, ns
        self._top_n = max(1, top_k) * PREFETCH_PER_SLOT
        self._task: asyncio.Task[_Prefetched | None] | None = None
        # served units whose path the head did not carry (disclosed when > 0)
        self.missed = 0
        # Where the served block came from ("" until annotate ran or when the table is
        # off) and how many served units were read live because their row was missing or
        # stale
        self.source = ""
        self.stale = 0

    @property
    def started(self) -> bool:
        return self._task is not None

    def start(self, candidates: list[CodeUnit]) -> None:
        if self._task is not None or not candidates:
            return
        seen: set[str] = set()
        head: list[CodeUnit] = []
        for u in candidates:
            key = u.unit_id or f"{u.path}:{u.start_line}"
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > self._top_n:
                break
            head.append(u)
        try:
            self._task = create_deferred_task(self._run(head))
        except Exception:  # noqa: BLE001 — no loop, no prefetch; the join reads sequentially
            self._task = None

    async def _run(self, head: list[CodeUnit]) -> _Prefetched | None:
        from server.code.app import unit_risk as code_unit_risk
        from server.code.app.spans import code_span

        try:
            with code_span("search.risk_prefetch", results=len(head)):
                rows = await code_unit_risk.read_rows(
                    self._repo, self._org, self._ns, [u.unit_id for u in head]
                )
                paths: list[str] = []
                for u in head:
                    if u.path and u.unit_id not in rows and u.path not in paths:
                        paths.append(u.path)
                facts = await _read_path_facts(self._repo, self._org, self._ns, paths)
                return _Prefetched(rows=rows, facts=facts, asked={u.unit_id for u in head})
        except Exception:  # the prefetch is an optimization: search runs without it
            logger.warning("code.search: risk prefetch failed", exc_info=True)
            return None

    def _disclose(self, units: list[CodeUnit], rows: dict[str, Any]) -> None:
        from_table = sum(1 for u in units if u.unit_id in rows)
        self.stale = len(units) - from_table
        self.source = (
            "table" if from_table == len(units) else "live" if from_table == 0 else "mixed"
        )

    async def annotate(self, units: list[CodeUnit], *, reserve_tokens: int) -> int:
        """Attach the annotation to the served `units`; returns tokens spent."""
        if not units:
            return 0
        from server.code.app import unit_risk as code_unit_risk

        pre = await self._task if self._task is not None else None
        if pre is None:
            self._disclose(units, {})
            return await annotate_units(
                self._repo, self._org, self._ns, units, reserve_tokens=reserve_tokens
            )
        # a served unit the head did not carry (a lifted anchor, a diversity
        # refill) gets its own keyed read before the live path round
        outside = [u.unit_id for u in units if u.unit_id and u.unit_id not in pre.asked]
        if outside:
            pre.rows.update(
                await code_unit_risk.read_rows(self._repo, self._org, self._ns, outside)
            )
        self._disclose(units, pre.rows)
        missed = [
            u
            for u in units
            if u.path and u.unit_id not in pre.rows and u.path not in pre.facts.paths
        ]
        if missed:
            self.missed = len(missed)
            extra = await _read_path_facts(
                self._repo, self._org, self._ns, list({u.path for u in missed})
            )
            pre.facts.merge(extra)
        return await _annotate_from(
            self._repo,
            self._org,
            self._ns,
            units,
            pre.facts,
            reserve_tokens=reserve_tokens,
            precomputed=pre.rows,
        )
