"""Read-only audit of the fast-tier call graph's two silent holes.

Edges are re-derived per path when THAT path changes. A definition that
appears or disappears in another file never re-resolves the sites that name
it, so two populations accumulate:

    A  live declined call sites (`code_call_sites_unresolved`) whose bare
       callee name now has a live definition in the index. Split by how
       many distinct definitions the name has today: exactly one (the
       resolver would bind it at 0.6 — the closure-repair population) or
       several (stays declined as `ambiguous`; only its count is stale).
       Reported for `unresolved` sites, and separately for `ambiguous`
       sites whose name has shrunk to one definition.
    B  live tree-sitter `calls` edges at confidence <= 0.6 (the repo-wide
       unique-name grade) whose `to_symbol_id` has no live `code_symbols`
       row: the target was removed or renamed and the edge outlived it.
       The same count at any confidence is printed for scale.

Every read is a namespace scan under FINAL — this is a census, not a hot
path; run it against a deployment with the background workers off:

    LOOM_BACKGROUND_WORKERS=off python -P -m scripts.ops.code_edge_audit
    LOOM_BACKGROUND_WORKERS=off python -P -m scripts.ops.code_edge_audit \
        --namespace code-clickhouse --json out.json

Settings come from the process environment / .env the way the server reads
them (server.core.ch.get_client). Nothing here writes to ClickHouse: every
statement goes through `_read`, which refuses anything but a SELECT. The
reads run as background traffic (lower priority, capped threads), each
bounded by `--max-query-s`, and are attributed in `system.query_log` with the
`log_comment` `ops.code_edge_audit <org>/<namespace>`. `--json` replaces its
file only once the whole report is in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

TAG = "ops.code_edge_audit"
_READ_LEAD = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

_MISS_TOTALS = """
SELECT disposition, count() AS n, uniqExact(callee_name) AS names
FROM loom.code_call_sites_unresolved FINAL
WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
GROUP BY disposition
"""

# Definitions are counted per distinct (qualified_name, symbol_id), both
# tiers folded — a symbol indexed by the parser and the compiler is one
# definition, which is how CodeRepo.name_candidates presents it.
_A = """
WITH defs AS (
    SELECT name, uniqExact((qualified_name, symbol_id)) AS n_defs
    FROM loom.code_symbols FINAL
    WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
    GROUP BY name
)
SELECT
    m.disposition AS disposition,
    count() AS sites,
    uniqExact(m.callee_name) AS names,
    countIf(d.n_defs = 1) AS sites_one_def,
    uniqExactIf(m.callee_name, d.n_defs = 1) AS names_one_def,
    countIf(d.n_defs > 1) AS sites_many_defs,
    uniqExactIf(m.callee_name, d.n_defs > 1) AS names_many_defs
FROM loom.code_call_sites_unresolved AS m FINAL
INNER JOIN defs AS d ON d.name = m.callee_name
WHERE m.org = {org:String} AND m.namespace = {ns:String} AND m.tombstoned_at IS NULL
GROUP BY m.disposition
"""

# `confidence` is Float32: the stored 0.6 is 0.6000000238, which is NOT
# <= the Float64 literal 0.6. The grade below 0.8 is 0.6, so `< 0.65` selects exactly it.
_EDGE_TOTALS = """
SELECT count() AS live_calls, countIf(conf < 0.65) AS live_calls_le_06
FROM (
    SELECT argMax(confidence, version) AS conf, argMax(tombstoned_at IS NULL, version) AS live
    FROM loom.code_edges
    WHERE org = {org:String} AND namespace = {ns:String}
      AND tier = 'treesitter' AND edge_kind = 'calls'
    GROUP BY tier, edge_kind, to_symbol_id, from_symbol_id, path, line
    HAVING live = 1
)
"""

_B = """
SELECT count() AS dangling_any, countIf(e.conf < 0.65) AS dangling_le_06,
       uniqExact(e.to_symbol_id) AS targets_any,
       uniqExactIf(e.to_symbol_id, e.conf < 0.65) AS targets_le_06,
       uniqExactIf(e.path, e.conf < 0.65) AS paths_le_06
FROM (
    SELECT to_symbol_id, path,
           argMax(confidence, version) AS conf, argMax(tombstoned_at IS NULL, version) AS live
    FROM loom.code_edges
    WHERE org = {org:String} AND namespace = {ns:String}
      AND tier = 'treesitter' AND edge_kind = 'calls'
    GROUP BY tier, edge_kind, to_symbol_id, from_symbol_id, path, line
    HAVING live = 1
) AS e
LEFT ANTI JOIN (
    SELECT DISTINCT symbol_id
    FROM loom.code_symbols FINAL
    WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
) AS s ON s.symbol_id = e.to_symbol_id
"""

# The name-keyed side table keeps every version ever inserted (tombstones
# too), so "has a row in code_symbol_names" over-counts definitions that
# were since removed; printed beside the live count for comparability.
_A_SIDE_TABLE = """
SELECT uniqExact(m.callee_name) AS names, count() AS sites
FROM loom.code_call_sites_unresolved AS m FINAL
INNER JOIN (
    SELECT DISTINCT name FROM loom.code_symbol_names
    WHERE org = {org:String} AND namespace = {ns:String}
) AS d ON d.name = m.callee_name
WHERE m.org = {org:String} AND m.namespace = {ns:String} AND m.tombstoned_at IS NULL
  AND m.disposition = 'unresolved'
"""

_NAMESPACES = """
SELECT org, namespace, count() AS rows
FROM loom.code_call_sites_unresolved
WHERE ({org:String} = '' OR org = {org:String})
  AND (empty({nss:Array(String)}) OR has({nss:Array(String)}, namespace))
GROUP BY org, namespace ORDER BY org, namespace
"""


async def _read(ch: Any, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run one SELECT; anything else is refused before it reaches the server."""
    if not _READ_LEAD.match(sql):
        raise ValueError(f"{TAG} only reads; refused: {sql.strip()[:60]!r}")
    rows: list[dict[str, Any]] = await ch.query_all(sql, params)
    return rows


async def audit_namespace(ch: Any, org: str, ns: str) -> dict[str, Any]:
    from server.core.ch import query_tag_var

    token = query_tag_var.set(f"{TAG} {org}/{ns}")
    try:
        return await _audit_namespace(ch, org, ns)
    finally:
        query_tag_var.reset(token)


async def _audit_namespace(ch: Any, org: str, ns: str) -> dict[str, Any]:
    p = {"org": org, "ns": ns}
    out: dict[str, Any] = {"org": org, "namespace": ns}
    t0 = time.monotonic()

    totals = {str(r["disposition"]): r for r in await _read(ch, _MISS_TOTALS, p)}
    out["misses"] = {
        d: {"sites": int(totals[d]["n"]), "names": int(totals[d]["names"])}
        if d in totals
        else {"sites": 0, "names": 0}
        for d in ("unresolved", "ambiguous")
    }

    a_rows = {str(r["disposition"]): r for r in await _read(ch, _A, p)}

    def _a(d: str) -> dict[str, int]:
        r = a_rows.get(d)
        keys = (
            "sites",
            "names",
            "sites_one_def",
            "names_one_def",
            "sites_many_defs",
            "names_many_defs",
        )
        return {k: int(r[k]) if r else 0 for k in keys}

    out["A_unresolved_with_definition"] = _a("unresolved")
    out["A_ambiguous_with_definition"] = _a("ambiguous")
    (side,) = await _read(ch, _A_SIDE_TABLE, p)
    out["A_unresolved_name_in_side_table"] = {
        "names": int(side["names"]),
        "sites": int(side["sites"]),
    }

    (et,) = await _read(ch, _EDGE_TOTALS, p)
    out["edges"] = {
        "live_calls": int(et["live_calls"]),
        "live_calls_le_06": int(et["live_calls_le_06"]),
    }
    (b,) = await _read(ch, _B, p)
    out["B_dangling_target"] = {
        k: int(b[k])
        for k in ("dangling_le_06", "targets_le_06", "paths_le_06", "dangling_any", "targets_any")
    }

    unres = out["misses"]["unresolved"]["sites"] or 1
    le06 = out["edges"]["live_calls_le_06"] or 1
    out["ratios"] = {
        "A_unresolved_sites_pct_of_unresolved": round(
            100.0 * out["A_unresolved_with_definition"]["sites"] / unres, 2
        ),
        "A_one_def_sites_pct_of_unresolved": round(
            100.0 * out["A_unresolved_with_definition"]["sites_one_def"] / unres, 2
        ),
        "B_le_06_pct_of_le_06_edges": round(
            100.0 * out["B_dangling_target"]["dangling_le_06"] / le06, 2
        ),
    }
    out["took_s"] = round(time.monotonic() - t0, 1)
    return out


def _print(rep: dict[str, Any]) -> None:
    a = rep["A_unresolved_with_definition"]
    a2 = rep["A_ambiguous_with_definition"]
    b = rep["B_dangling_target"]
    m = rep["misses"]
    e = rep["edges"]
    print(f"\n== {rep['org']}/{rep['namespace']}  ({rep['took_s']} s)")
    print(
        f"  live misses: unresolved {m['unresolved']['sites']:,} sites / "
        f"{m['unresolved']['names']:,} names; ambiguous {m['ambiguous']['sites']:,} / "
        f"{m['ambiguous']['names']:,}"
    )
    r = rep["ratios"]
    print(
        f"  A  unresolved sites whose name now has a definition: {a['sites']:,} sites / "
        f"{a['names']:,} names ({r['A_unresolved_sites_pct_of_unresolved']}% of unresolved)"
    )
    print(
        f"     exactly one definition (resolvable at 0.6): {a['sites_one_def']:,} sites / "
        f"{a['names_one_def']:,} names ({r['A_one_def_sites_pct_of_unresolved']}%)"
    )
    print(
        f"     several definitions (stay declined, count stale): "
        f"{a['sites_many_defs']:,} sites / {a['names_many_defs']:,} names"
    )
    print(
        f"     ambiguous sites whose name shrank to one definition: "
        f"{a2['sites_one_def']:,} sites / {a2['names_one_def']:,} names"
    )
    st = rep["A_unresolved_name_in_side_table"]
    print(
        f"     (name has ANY version in code_symbol_names, live or not: {st['sites']:,} sites / "
        f"{st['names']:,} names)"
    )
    print(f"  live parser calls edges: {e['live_calls']:,}; at <= 0.6: {e['live_calls_le_06']:,}")
    print(
        f"  B  <= 0.6 edges whose target has no live symbol: {b['dangling_le_06']:,} edges / "
        f"{b['targets_le_06']:,} targets / {b['paths_le_06']:,} paths "
        f"({rep['ratios']['B_le_06_pct_of_le_06_edges']}% of <= 0.6 edges); "
        f"any confidence: {b['dangling_any']:,} edges / {b['targets_any']:,} targets"
    )


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--org", default="")
    ap.add_argument(
        "--namespace",
        action="append",
        default=[],
        help="restrict to this namespace (repeatable); default: every "
        "namespace with declined sites",
    )
    ap.add_argument("--json", default="", help="also write the report here")
    ap.add_argument(
        "--max-query-s",
        type=float,
        default=600.0,
        help="deadline of each read (the server stops it past this); default 600",
    )
    return ap


async def audit(ch: Any, args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """The report over every matching namespace; (1, {}) when none match."""
    (ver,) = await _read(ch, "SELECT version() AS v")
    print(f"server {ver['v']}")
    rows = await _read(ch, _NAMESPACES, {"org": args.org, "nss": list(args.namespace)})
    targets = [(str(r["org"]), str(r["namespace"])) for r in rows]
    if not targets:
        print("no namespaces with declined call sites match", file=sys.stderr)
        return 1, {}
    report: list[dict[str, Any]] = []
    for org, ns in targets:
        rep = await audit_namespace(ch, org, ns)
        _print(rep)
        report.append(rep)
    return 0, {"server": ver["v"], "namespaces": report}


async def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    from server.core.ch import get_client, query_tag_var, statement_timeout
    from server.observability.traffic_class import bind_background, reset_traffic_class
    from server.settings import settings

    print(f"ClickHouse {settings.ch_host}:{settings.ch_port} database {settings.ch_database}")
    ch = get_client()
    await ch.connect()
    cls, tag = bind_background(f"cli:{TAG}"), query_tag_var.set(TAG)
    try:
        with statement_timeout(args.max_query_s):
            return await audit(ch, args)
    finally:
        query_tag_var.reset(tag)
        reset_traffic_class(cls)
        await ch.disconnect()


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Replace `path` with the report in one rename, so a reader never sees half."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(report, indent=1, default=str, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rc, report = asyncio.run(run(args))
    if rc == 0 and args.json:
        write_report(Path(args.json), report)
        print(f"\nwrote {args.json}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
