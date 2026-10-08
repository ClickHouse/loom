# ruff: noqa: E501
"""Do the lessons a run relied on go with graded passes?

Read-only. Uses the server's ClickHouse settings (LOOM_CH_*), so run it where
the server runs (e.g. inside the workers container):

    python bench/lesson_grade_attribution.py --org <org> --namespace clickgap-public
    python bench/lesson_grade_attribution.py --org <org> --namespace clickgap-public --json

Reports, for one namespace:
  coverage   closed runs, runs that named memories_used, runs graded (pass / fail),
             graded runs that named memories_used — the population the answer rests on
  lift       pass rate of graded runs that used a memory vs graded runs that did not,
             per memory with at least --min-runs graded uses (Wilson 95% bounds), and
             the same split for proposals vs accepted facts
  verdict    INSUFFICIENT when fewer than --min-graded graded runs named memories_used:
             the question cannot be answered yet, and the report says what is missing

A lift is a correlation over whatever runs were graded, not a controlled trial:
it is the signal to put a lesson on trial (memory.set proposed=true), not the
decision itself (that stays with the namespace owner).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from typing import Any

_COVERAGE = """
SELECT count() AS closed,
       countIf(notEmpty(memories_used)) AS with_used,
       countIf(grade = 'pass') AS pass_runs,
       countIf(grade = 'fail') AS fail_runs,
       countIf(grade != '' AND notEmpty(memories_used)) AS graded_with_used,
       toString(max(graded_at)) AS last_graded_at,
       toString(min(started_at)) AS first_run
FROM loom.agent_runs FINAL
WHERE org = {org:String} AND namespace = {ns:String} AND status != 'active'
"""

# Per memory: graded runs that used it, and how many of those passed.
_PER_KEY = """
SELECT key, count() AS runs, countIf(grade = 'pass') AS passes
FROM loom.agent_runs FINAL
ARRAY JOIN memories_used AS key
WHERE org = {org:String} AND namespace = {ns:String} AND grade IN ('pass', 'fail')
GROUP BY key
HAVING runs >= {min_runs:UInt32}
ORDER BY runs DESC
LIMIT 200
"""

_BASE = """
SELECT count() AS runs, countIf(grade = 'pass') AS passes
FROM loom.agent_runs FINAL
WHERE org = {org:String} AND namespace = {ns:String} AND grade IN ('pass', 'fail')
"""

_STATUS = """
SELECT memory_key, toString(argMax(status, version)) AS status
FROM loom.memory FINAL
WHERE org = {org:String} AND namespace = {ns:String} AND memory_key IN {keys:Array(String)}
GROUP BY memory_key
"""


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a pass rate; (0, 1) with no runs."""
    if n <= 0:
        return 0.0, 1.0
    p = passes / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d), min(1.0, (c + r) / d)


def summarise(
    cov: dict[str, Any],
    base: dict[str, Any],
    per_key: list[dict[str, Any]],
    status: dict[str, str],
    *,
    min_graded: int,
) -> dict[str, Any]:
    graded_used = int(cov.get("graded_with_used") or 0)
    total_runs, total_pass = int(base.get("runs") or 0), int(base.get("passes") or 0)
    rows = []
    for r in per_key:
        n, k = int(r["runs"]), int(r["passes"])
        rest_n, rest_k = total_runs - n, total_pass - k
        lo, hi = wilson(k, n)
        rows.append(
            {
                "memory_key": r["key"],
                "status": status.get(str(r["key"]), "?"),
                "runs": n,
                "pass_rate": round(k / n, 3) if n else 0.0,
                "pass_rate_ci95": [round(lo, 3), round(hi, 3)],
                "rest_pass_rate": round(rest_k / rest_n, 3) if rest_n else None,
                "lift": round(k / n - rest_k / rest_n, 3) if n and rest_n else None,
            }
        )
    missing = []
    if int(cov.get("pass_runs") or 0) + int(cov.get("fail_runs") or 0) == 0:
        missing.append("no run is graded: call run.grade when a verdict arrives")
    if int(cov.get("with_used") or 0) == 0:
        missing.append("no run names memories_used: pass the keys relied on to run.complete")
    return {
        "coverage": cov,
        "baseline": {
            "graded_runs": total_runs,
            "pass_rate": round(total_pass / total_runs, 3) if total_runs else None,
        },
        "verdict": "OK" if graded_used >= min_graded else "INSUFFICIENT",
        "missing": missing,
        "per_memory": rows,
    }


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--org", required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument(
        "--min-runs", type=int, default=5, help="graded uses a memory needs to be listed"
    )
    ap.add_argument(
        "--min-graded", type=int, default=30, help="graded runs with memories_used for a verdict"
    )
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    p = {"org": a.org, "ns": a.namespace}
    cov = (await ch.query_all(_COVERAGE, params=p) or [{}])[0]
    base = (await ch.query_all(_BASE, params=p) or [{}])[0]
    per_key = await ch.query_all(_PER_KEY, params={**p, "min_runs": a.min_runs})
    keys = [str(r["key"]) for r in per_key]
    status = {
        str(r["memory_key"]): str(r["status"])
        for r in (await ch.query_all(_STATUS, params={**p, "keys": keys}) if keys else [])
    }
    out = summarise(cov, base, per_key, status, min_graded=a.min_graded)
    if a.json:
        print(json.dumps(out, default=str, indent=2))
        return
    c = out["coverage"]
    print(
        f"{a.org}/{a.namespace}: {c.get('closed')} closed runs since {c.get('first_run')}, "
        f"{c.get('with_used')} name memories_used; graded {c.get('pass_runs')} pass / {c.get('fail_runs')} fail "
        f"(last {c.get('last_graded_at') or 'never'}); {c.get('graded_with_used')} graded runs name memories_used"
    )
    print(f"verdict: {out['verdict']}  baseline pass rate: {out['baseline']['pass_rate']}")
    for m in out["missing"]:
        print(f"  missing: {m}")
    for r in out["per_memory"][:30]:
        print(
            f"  {r['memory_key'][:48]:48} {r['status']:10} n={r['runs']:4} pass={r['pass_rate']:.2f} "
            f"ci95={r['pass_rate_ci95']} lift={r['lift']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
