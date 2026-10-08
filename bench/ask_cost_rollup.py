"""What a memory.ask outcome costs, per consumer per day.

The ledger (`loom.agent_events`) has the dollars and the outcome of every ask
(`args.out_source`: synthesized / abstained / retrieval_only / record). The usage plane
(`loom.memory_usage_events`) has what the agent then relied on (`used`) and what was
confirmed (`evidence`). This joins them, so "what did a correct answer cost" has a
number the ask cascade can be measured against.

The two tables share no request id, so the join is by (org, namespace, agent,
day) — coarse, but that is the grain a budget is set at. Per row:

    asks, synthesized, abstained, retrieval_only, usd, llm_in, llm_out,
    used, evidence, usd_per_synthesized, usd_per_used, abstain_rate

Read-only. Needs LOOM_CH_* in the environment or a dotenv (`--env-file`).

    python bench/ask_cost_rollup.py --days 7 --min-asks 20
    python bench/ask_cost_rollup.py --days 1 --org <org> --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

KEY = ("org", "namespace", "agent", "day")


def ask_sql(days: int, org: str | None) -> str:
    org_pred = "AND org = {org:String}" if org else ""
    return f"""
        SELECT org, namespace, agent, toDate(ts) AS day,
               count() AS asks,
               countIf(args['out_source'] = 'synthesized') AS synthesized,
               countIf(args['out_source'] = 'abstained'
                       OR args['out_abstain'] = 'true') AS abstained,
               countIf(args['out_source'] = 'retrieval_only') AS retrieval_only,
               sum(upstream_cost_usd) AS usd,
               sum(input_tokens) AS llm_in, sum(output_tokens) AS llm_out
        FROM loom.agent_events
        WHERE ts > now() - INTERVAL {int(days)} DAY AND operation = 'memory.ask' AND success
          {org_pred}
        GROUP BY org, namespace, agent, day
    """


def usage_sql(days: int, org: str | None) -> str:
    org_pred = "AND org = {org:String}" if org else ""
    return f"""
        SELECT org, namespace, agent, toDate(ts) AS day,
               sumIf(event_count, event_type IN ('used', 'used_cited')) AS used,
               sumIf(event_count, event_type = 'evidence') AS evidence
        FROM loom.memory_usage_events
        WHERE ts > now() - INTERVAL {int(days)} DAY
          AND event_type IN ('used', 'used_cited', 'evidence')
          {org_pred}
        GROUP BY org, namespace, agent, day
    """


def rollup(
    ask_rows: list[dict[str, Any]], usage_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Join the two aggregates on (org, namespace, agent, day) and derive the
    per-outcome costs. A day with usage but no asks is kept (used > 0, asks 0)
    so a consumer relying on memory it reads through search still shows."""
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in ask_rows:
        k = tuple(str(r[c]) for c in KEY)
        by_key[k] = {
            **{c: str(r[c]) for c in KEY},
            **{
                f: (float(r.get(f) or 0.0) if f == "usd" else int(r.get(f) or 0))
                for f in (
                    "asks",
                    "synthesized",
                    "abstained",
                    "retrieval_only",
                    "usd",
                    "llm_in",
                    "llm_out",
                )
            },
            "used": 0,
            "evidence": 0,
        }
    for r in usage_rows:
        k = tuple(str(r[c]) for c in KEY)
        row = by_key.setdefault(
            k,
            {
                **{c: str(r[c]) for c in KEY},
                "asks": 0,
                "synthesized": 0,
                "abstained": 0,
                "retrieval_only": 0,
                "usd": 0.0,
                "llm_in": 0,
                "llm_out": 0,
                "used": 0,
                "evidence": 0,
            },
        )
        row["used"] += int(r.get("used") or 0)
        row["evidence"] += int(r.get("evidence") or 0)
    out: list[dict[str, Any]] = []
    for row in by_key.values():
        usd = float(row["usd"])
        row["usd"] = round(usd, 4)
        row["usd_per_synthesized"] = (
            round(usd / row["synthesized"], 4) if row["synthesized"] else None
        )
        row["usd_per_used"] = round(usd / row["used"], 4) if row["used"] else None
        row["abstain_rate"] = round(row["abstained"] / row["asks"], 3) if row["asks"] else None
        out.append(row)
    out.sort(key=lambda r: (-r["usd"], r["org"], r["namespace"], r["agent"], r["day"]))
    return out


def _client(env_file: str | None) -> Any:
    env = dict(os.environ)
    if env_file and not Path(env_file).exists():
        raise SystemExit(f"--env-file {env_file} does not exist")
    if env_file:
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


def _rows(ch: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    res = ch.query(sql, parameters=params)
    return [dict(zip(res.column_names, r, strict=False)) for r in res.result_rows]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--org", default="")
    ap.add_argument("--min-asks", type=int, default=0, help="hide rows with fewer asks")
    ap.add_argument(
        "--env-file", default="", help="dotenv with LOOM_CH_*; default: the environment only"
    )
    ap.add_argument("--json", default="", help="write the rows here as JSON")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    ch = _client(args.env_file)
    params = {"org": args.org} if args.org else {}
    rows = rollup(
        _rows(ch, ask_sql(args.days, args.org or None), params),
        _rows(ch, usage_sql(args.days, args.org or None), params),
    )
    rows = [r for r in rows if r["asks"] >= args.min_asks]
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, default=str))
    print(
        f"{'day':10} {'org':10} {'namespace':24} {'agent':14} {'asks':>5} {'synth':>5} {'abst':>5} "
        f"{'used':>6} {'usd':>8} {'$/synth':>8} {'$/used':>8}"
    )
    for r in rows[: args.top]:
        per_synth = r["usd_per_synthesized"]
        per_used = r["usd_per_used"]
        print(
            f"{r['day']:10} {r['org'][:10]:10} {r['namespace'][:24]:24} {r['agent'][:14]:14} "
            f"{r['asks']:5d} {r['synthesized']:5d} {r['abstained']:5d} {r['used']:6d} "
            f"{r['usd']:8.2f} {(per_synth if per_synth is not None else float('nan')):8.4f} "
            f"{(per_used if per_used is not None else float('nan')):8.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
