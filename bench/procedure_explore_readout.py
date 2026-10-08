#!/usr/bin/env python3
"""What did showing a procedure do to the run?

Joins `context.pack` events — `out_procedure_keys` (shown) and
`out_procedure_keys_withheld` (matched, withheld by the exploration rate) —
to the judged outcome of the session they were packed for
(`loom.session_outcomes_by_user`), and prints per procedure:

    shown n / ok      withheld n / ok      P(ok|shown) − P(ok|withheld)  [Wilson 95% on each]

A positive gap with intervals that do not overlap is the evidence a utility
weight can be set from. Read-only; needs LOOM_CH_* in the
environment or --env-file.

    python bench/procedure_explore_readout.py --org <org> --days 30
    python bench/procedure_explore_readout.py --org <org> --namespace ns --days 7 --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

SQL = """
WITH packs AS (
    SELECT org, namespace, session_id,
           arrayFilter(x -> x != '',
                       splitByChar(',', args['out_procedure_keys'])) AS shown,
           arrayFilter(x -> x != '',
                       splitByChar(',', args['out_procedure_keys_withheld'])) AS withheld
    FROM loom.agent_events
    WHERE ts > now() - INTERVAL {days:UInt32} DAY
      AND operation = 'context.pack' AND success
      AND session_id != '' AND session_id != '-'
      AND org = {org:String}
      AND ({ns:String} = '*' OR namespace = {ns:String})
      AND (mapContains(args, 'out_procedure_keys')
           OR mapContains(args, 'out_procedure_keys_withheld'))
),
judged AS (
    SELECT org, namespace, session_id, argMax(outcome, judged_at) AS outcome
    FROM loom.session_outcomes_by_user
    WHERE judged_at > now() - INTERVAL {days:UInt32} DAY AND org = {org:String}
    GROUP BY org, namespace, session_id
),
exposures AS (
    SELECT p.namespace AS namespace, key, 'shown' AS arm, j.outcome AS outcome
    FROM packs p ARRAY JOIN shown AS key
    INNER JOIN judged j
        ON j.org = p.org AND j.namespace = p.namespace AND j.session_id = p.session_id
    UNION ALL
    SELECT p.namespace AS namespace, key, 'withheld' AS arm, j.outcome AS outcome
    FROM packs p ARRAY JOIN withheld AS key
    INNER JOIN judged j
        ON j.org = p.org AND j.namespace = p.namespace AND j.session_id = p.session_id
)
SELECT namespace, key,
       countIf(arm = 'shown') AS shown_n,
       countIf(arm = 'shown' AND outcome = 'positive') AS shown_ok,
       countIf(arm = 'withheld') AS withheld_n,
       countIf(arm = 'withheld' AND outcome = 'positive') AS withheld_ok
FROM exposures
WHERE outcome IN ('positive', 'negative')
GROUP BY namespace, key
ORDER BY shown_n + withheld_n DESC, namespace, key
LIMIT {lim:UInt32}
"""


REUSE_SQL = """
SELECT
    (SELECT count() FROM loom.agent_events
      WHERE ts > now() - INTERVAL {days:UInt32} DAY AND operation = 'context.pack' AND success
        AND org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})) AS packs,
    (SELECT count() FROM loom.agent_events
      WHERE ts > now() - INTERVAL {days:UInt32} DAY AND operation = 'context.pack' AND success
        AND org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})
        AND args['out_procedure_keys'] != '') AS packs_showing,
    (SELECT count() FROM loom.agent_events
      WHERE ts > now() - INTERVAL {days:UInt32} DAY AND operation = 'context.pack' AND success
        AND org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})
        AND args['out_procedure_keys_withheld'] != '') AS packs_withholding,
    (SELECT count() FROM loom.procedure_runs
      WHERE ts > now() - INTERVAL {days:UInt32} DAY
        AND org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})) AS ledger_rows,
    (SELECT uniq(memory_key) FROM loom.procedure_runs
      WHERE ts > now() - INTERVAL {days:UInt32} DAY
        AND org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})) AS procedures_run
"""

RETENTION_SQL = """
SELECT namespace, memory_key,
       count() AS runs, countIf(run_ok) AS ok,
       countIf(prev_ok) AS pairs, countIf(prev_ok AND run_ok) AS ok_after_ok
FROM (
    SELECT namespace, memory_key, ok AS run_ok,
           lagInFrame(ok) OVER (PARTITION BY namespace, memory_key ORDER BY ts) AS prev_ok,
           row_number() OVER (PARTITION BY namespace, memory_key ORDER BY ts) AS rn
    FROM loom.procedure_runs
    WHERE ts > now() - INTERVAL {days:UInt32} DAY
      AND org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})
)
WHERE rn > 1
GROUP BY namespace, memory_key
ORDER BY runs DESC
LIMIT {lim:UInt32}
"""


def retention_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per procedure: success rate and success retention P(ok_{t+1} | ok_t),
    from ledger rows already paired by SQL."""
    out: list[dict[str, Any]] = []
    for r in rows:
        runs, ok = int(r["runs"]), int(r["ok"])
        pairs, both = int(r["pairs"]), int(r["ok_after_ok"])
        out.append(
            {
                "namespace": r["namespace"],
                "key": r["memory_key"],
                "runs": runs + 1,
                "success": round(ok / runs, 3) if runs else None,
                "pairs": pairs,
                "retention": round(both / pairs, 3) if pairs else None,
            }
        )
    return out


def wilson(ok: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """(point, low, high) of the Wilson score interval; (0, 0, 0) at n = 0."""
    if n <= 0:
        return 0.0, 0.0, 0.0
    p = ok / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def readout(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per procedure: both arms, the gap, and whether the intervals separate."""
    out: list[dict[str, Any]] = []
    for r in rows:
        sn, so = int(r["shown_n"]), int(r["shown_ok"])
        wn, wo = int(r["withheld_n"]), int(r["withheld_ok"])
        sp, sl, sh = wilson(so, sn)
        wp, wl, wh = wilson(wo, wn)
        gap = (sp - wp) if (sn and wn) else None
        separated = bool(sn and wn and (sl > wh or sh < wl))
        out.append(
            {
                "namespace": r["namespace"],
                "key": r["key"],
                "shown_n": sn,
                "shown_ok": so,
                "shown_p": round(sp, 3),
                "shown_ci": [round(sl, 3), round(sh, 3)],
                "withheld_n": wn,
                "withheld_ok": wo,
                "withheld_p": round(wp, 3),
                "withheld_ci": [round(wl, 3), round(wh, 3)],
                "gap": round(gap, 3) if gap is not None else None,
                "separated": separated,
            }
        )
    return out


def _client(env_file: str | None) -> Any:
    env = dict(os.environ)
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip("'").strip('"'))
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=env["LOOM_CH_HOST"],
        port=int(env.get("LOOM_CH_PORT", "8443")),
        username=env["LOOM_CH_USER"],
        password=env["LOOM_CH_PASSWORD"],
        database=env.get("LOOM_CH_DATABASE", "loom"),
        secure=True,
        settings={"readonly": 1, "max_execution_time": 120},
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--org", required=True)
    ap.add_argument("--namespace", default="*")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--reuse",
        action="store_true",
        help="also print reuse (packs showing / withholding, ledger rows) and "
        "per-procedure success retention from the ledger",
    )
    a = ap.parse_args()
    ch = _client(a.env_file)
    if a.reuse:
        params = {"org": a.org, "ns": a.namespace, "days": a.days, "lim": a.limit}
        r = ch.query(REUSE_SQL, parameters=params)
        row = dict(zip(r.column_names, r.result_rows[0], strict=False)) if r.result_rows else {}
        packs = int(row.get("packs") or 0)
        showing = int(row.get("packs_showing") or 0)
        share = f" ({100.0 * showing / packs:.1f}%)" if packs else ""
        print(
            f"reuse ({a.days} d): packs {packs}, showing a procedure {showing}{share}, "
            f"withholding {int(row.get('packs_withholding') or 0)}, "
            f"ledger rows {int(row.get('ledger_rows') or 0)} over "
            f"{int(row.get('procedures_run') or 0)} procedures"
        )
        rr = ch.query(RETENTION_SQL, parameters=params)
        ret = retention_rows([dict(zip(rr.column_names, x, strict=False)) for x in rr.result_rows])
        for t in ret:
            print(
                f"  {t['namespace'][:24]:24s} {t['key'][:40]:40s} runs {t['runs']:>4d} "
                f"success {t['success']}  retention {t['retention']} over {t['pairs']} pairs"
            )
        if not ret:
            print("  no procedure with two or more ledger rows in the window")
        print()
    res = ch.query(
        SQL, parameters={"org": a.org, "ns": a.namespace, "days": a.days, "lim": a.limit}
    )
    rows = [dict(zip(res.column_names, r, strict=False)) for r in res.result_rows]
    table = readout(rows)
    if a.json:
        print(json.dumps(table, indent=2))
        return 0
    if not table:
        print(
            "no judged sessions with a packed procedure in the window "
            "(packs need a session_id; the exploration rate needs to be > 0 for a withheld arm)"
        )
        return 0
    print(f"{'namespace':24s} {'procedure':40s} {'shown':>12s} {'withheld':>12s} {'gap':>7s}  sep")
    for r in table:
        shown = f"{r['shown_ok']}/{r['shown_n']}"
        held = f"{r['withheld_ok']}/{r['withheld_n']}"
        gap = "" if r["gap"] is None else f"{r['gap']:+.3f}"
        print(
            f"{r['namespace'][:24]:24s} {r['key'][:40]:40s} {shown:>12s} {held:>12s} {gap:>7s}  "
            f"{'yes' if r['separated'] else ''}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
