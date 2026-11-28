"""The tests the index links to a set of touched definitions, and what a coverage run
measured of each: the reverse walk through the call graph, the test files that call the
other definitions of the edited files, and the imported execution evidence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from server.code.app.coverage_freshness import measured_definitions, partition_coverage_rows
from server.code.app.symbol.edges import _GRAPH_EDGE_KINDS
from server.code.domain.index import coverage_runs as cr
from server.code.store.index import CodeRepo
from server.code.store.index import prefer_scip_edges as _prefer_scip_edges

# A Python class call ends at the class symbol: a test that builds the object runs these.
_CONSTRUCTORS = ("__init__", "__new__")


@dataclass
class Links:
    # reverse-edge reach: {symbol_id: {qualified_name, path, hop, tier, roots, [via]}}, where
    # roots is the touched symbol ids the node reaches and via is `constructor`, `enclosing`
    # or `fixture`
    impacted: dict[str, dict[str, Any]] = field(default_factory=dict)
    # True when a hop's read hit its row limit
    capped: bool = False
    # {path, callees}: test files calling other definitions of the edited files
    neighbors: list[dict[str, Any]] = field(default_factory=list)
    # touched symbol_id -> {executed, commit}, for the definitions a verified run measured
    measured: dict[str, dict[str, Any]] = field(default_factory=dict)
    # {test_path, roots}: tests a verified run measured executing touched definitions
    coverage: list[dict[str, Any]] = field(default_factory=list)


def _seed_roots(
    touched: list[dict[str, Any]], symbol_rows: list[dict[str, Any]]
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """(seed id -> the touched symbol ids it stands for, seed id -> how it stands for a
    definition it is not). Every tier's id of a touched definition seeds it. Two kinds of
    definition have no callers of their own in a call graph, so another definition's
    callers are theirs: the class of a touched `__init__` or `__new__` (`constructor`),
    and every function a touched nested function is defined in (`enclosing`)."""
    roots: dict[str, set[str]] = {}
    for r in touched:
        for sid in r.get("tier_ids") or [r["symbol_id"]]:
            roots.setdefault(str(sid), set()).add(str(r["symbol_id"]))
    own = set(roots)
    by_name = {(str(r["path"]), str(r["qualified_name"])): r for r in symbol_rows}
    stands: dict[str, str] = {}

    def alias(row: dict[str, Any], root: str, how: str) -> None:
        for sid in row.get("tier_ids") or [row["symbol_id"]]:
            roots.setdefault(str(sid), set()).add(root)
            if str(sid) not in own:
                stands.setdefault(str(sid), how)

    for r in touched:
        owner, _, name = str(r["qualified_name"]).rpartition("::")
        parent = by_name.get((str(r["path"]), owner))
        if parent is not None and str(parent.get("kind")) == "class":
            if name in _CONSTRUCTORS:
                alias(parent, str(r["symbol_id"]), "constructor")
            continue
        while parent is not None and str(parent.get("kind")) != "class":
            alias(parent, str(r["symbol_id"]), "enclosing")
            owner = owner.rpartition("::")[0]
            parent = by_name.get((str(r["path"]), owner))
    return roots, stands


async def _walk(
    repo: CodeRepo,
    org: str,
    ns: str,
    roots: dict[str, set[str]],
    stands: dict[str, str],
    *,
    depth: int,
    read_limit: int,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Reverse edges from the seeds, depth <= 3. Every reached node remembers which touched
    symbols it reaches (`roots`), so a test is credited to the definitions it links to.
    Returns (impacted, capped): capped when a hop's read hit `read_limit`."""
    roots_of = {sid: set(ids) for sid, ids in roots.items()}
    impacted: dict[str, dict[str, Any]] = {}
    frontier = list(roots)
    capped = False
    for hop in range(max(1, min(depth, 3))):
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
        nxt: list[str] = []
        for r in _prefer_scip_edges(rows):
            sid, to = str(r["from_symbol_id"]), str(r["to_symbol_id"])
            if sid in roots and sid not in impacted:
                continue  # a seed calling a seed
            if sid not in impacted:
                impacted[sid] = {
                    "qualified_name": str(r["from_qualified_name"]),
                    "path": str(r["path"]),
                    "hop": hop + 1,
                    "tier": str(r["tier"]),
                    "roots": [],
                }
                if str(r.get("edge_kind")) == "fixture_use":
                    impacted[sid]["via"] = "fixture"
                elif to in stands:
                    impacted[sid]["via"] = stands[to]
                nxt.append(sid)
            merged = roots_of.setdefault(sid, set())
            merged |= roots_of.get(to) or set()
            impacted[sid]["roots"] = sorted(merged)
        frontier = nxt
    return impacted, capped


async def _coverage(
    repo: CodeRepo, org: str, ns: str, touched: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """(what a verified coverage run measured of each touched definition, the tests it
    measured executing them). The definitions' identity is settled by
    `measured_definitions`; a test row also needs its own file at the run's commit."""
    measured = await measured_definitions(repo, org, ns, touched)
    executed = [r for r in touched if measured.get(str(r["symbol_id"]), {}).get("executed")]
    if not executed:
        return measured, []
    paths = list(dict.fromkeys(str(r["path"]) for r in executed))
    current, _ = await partition_coverage_rows(
        repo, org, ns, await repo.coverage_rows_for(org, ns, paths)
    )
    tests: dict[str, set[str]] = {}
    for row in current:
        ranges = cr.evidence_ranges(str(row.get("evidence") or ""))
        for r in executed:
            if str(r["path"]) == str(row.get("src_path")) and cr.intersects(
                ranges, int(r["start_line"] or 0), int(r["end_line"] or 0)
            ):
                tests.setdefault(str(row.get("test_path") or ""), set()).add(str(r["symbol_id"]))
    return measured, [{"test_path": tp, "roots": sorted(ids)} for tp, ids in tests.items() if tp]


async def link_tests(
    repo: CodeRepo,
    org: str,
    ns: str,
    touched: list[dict[str, Any]],
    symbol_rows: list[dict[str, Any]],
    *,
    depth: int,
    read_limit: int,
    neighbors: int,
) -> Links:
    """The test links of `touched` (rows of `symbol_rows`, the definitions of the edited
    source files): the walk, at most `neighbors` file neighbours and the measured status,
    read together. A change that touches no definition (module-level lines) still has file
    neighbours."""
    if not symbol_rows:
        return Links()
    roots, stands = _seed_roots(touched, symbol_rows)
    others = [str(r["symbol_id"]) for r in symbol_rows if str(r["symbol_id"]) not in roots]
    (impacted, capped), near, (measured, coverage) = await asyncio.gather(
        _walk(repo, org, ns, roots, stands, depth=depth, read_limit=read_limit),
        repo.test_files_calling(org, ns, others, limit=neighbors),
        _coverage(repo, org, ns, touched),
    )
    return Links(
        impacted=impacted,
        capped=capped,
        neighbors=[{"path": str(r["path"]), "callees": int(r["callees"])} for r in near],
        measured=measured,
        coverage=coverage,
    )
