"""tracker.similar on its gold: the public issues the tracker
holds as closed duplicates whose `related_to` edge names an OLDER issue, replayed
against a running loom with `issue_number` — the op walls time at the item's
creation, so the target is found or not the way it would have been on the day
the duplicate was filed. Arms are the legs, one at a time, paired per query.

Gold comes from the QA database through loom's own read-only client
(`server.tracker.store.source.qa_intel`, one credential); the op calls go
over HTTP to `--base-url` with `--token`. Nothing is written anywhere.

Run:
  python bench/tracker_similar_bench.py --base-url http://127.0.0.1:7953 \
      --token-file out/bench.token \
      --env-file .env --out out/bench.json

`--env-file` names the env whose tracker settings and QA credential the gold
query reads; it is required unless `--pairs-file` supplies the gold, and nothing
is read from a default path.

Prints hit@1 / hit@5 / hit@10 / MRR per arm and the wall p50 / p90 / p95 of
the op call (client-side, one call at a time), the same table over the
queries whose body does NOT name a target (`query.mentions` on the op's own
answer — where only meaning can find it), and writes the
per-query rows as JSON. Arms include the vector arms (`vector`,
`lexical+fields+vector`, `+vector`, `default+vector`), `--pairs-file` (replay
the recorded pairs exactly) and `--gold related` (the secondary
stratum: issues not closed as duplicates with a `related_to` edge to an
older issue — "same pattern", never headlined).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# the default arm runs FIRST per query so its wall is the uncached one (the
# source caches a statement for five minutes; the later arms re-read the same
# item, lexical and frame rows from the cache and their walls are lower bounds)
ARMS: dict[str, dict[str, Any]] = {
    # `legs: []` follows LOOM_TRACKER_SIMILAR_VECTOR_LEG — the default set when it is off
    "default (issue+pr)": {"legs": [], "kinds": ["issue", "pr"]},
    # The default set WITH the vector leg, recall-only fused
    "default+vector (issue+pr)": {
        "legs": ["lexical", "fields", "frames", "mentions", "vector"],
        "kinds": ["issue", "pr"],
    },
    "lexical": {"legs": ["lexical"], "kinds": ["issue"]},
    "lexical+fields": {"legs": ["lexical", "fields"], "kinds": ["issue"]},
    "lexical+fields+frames": {"legs": ["lexical", "fields", "frames"], "kinds": ["issue"]},
    "+mentions": {"legs": ["lexical", "fields", "frames", "mentions"], "kinds": ["issue"]},
    # The vector leg over loom's own embeddings, alone, beside
    # the lexical leg, and on top of every other leg (kinds issue)
    "vector": {"legs": ["vector"], "kinds": ["issue"]},
    "lexical+fields+vector": {"legs": ["lexical", "fields", "vector"], "kinds": ["issue"]},
    "+vector": {"legs": ["lexical", "fields", "frames", "mentions", "vector"], "kinds": ["issue"]},
    # the head a caller asking for five sees
    "+vector (top_k 5)": {
        "legs": ["lexical", "fields", "frames", "mentions", "vector"],
        "kinds": ["issue"],
        "top_k": 5,
    },
    "default+vector (issue+pr, top_k 5)": {
        "legs": ["lexical", "fields", "frames", "mentions", "vector"],
        "kinds": ["issue", "pr"],
        "top_k": 5,
    },
}

# the secondary stratum: issues NOT closed as duplicates with a
# `related_to` edge to an older issue — "same pattern" pairs, never headlined
RELATED_SQL = """
SELECT d.source_id AS dup, toUInt32(splitByChar('#', d.source_id)[2]) AS dup_number,
       d.created_at AS dup_created, d.is_crash_ci AS crash,
       groupArray(toUInt32(splitByChar('#', t.source_id)[2])) AS targets
FROM (SELECT id, source_id, created_at, is_crash_ci FROM {db}.github_issues FINAL
      WHERE is_private = 0 AND state_reason != 'duplicate'
        AND splitByChar('#', source_id)[1] = {{repo:String}}) AS d
JOIN {db}.edges AS e ON e.from_id = d.id AND e.rel_type = 'related_to'
JOIN (SELECT id, source_id, created_at FROM {db}.github_issues FINAL WHERE is_private = 0) AS t
  ON t.id = e.to_id
WHERE t.created_at < d.created_at
GROUP BY dup, dup_number, dup_created, crash
ORDER BY dup_created DESC
LIMIT {{limit:UInt32}}
"""

GOLD_SQL = """
SELECT d.source_id AS dup, toUInt32(splitByChar('#', d.source_id)[2]) AS dup_number,
       d.created_at AS dup_created, d.is_crash_ci AS crash,
       groupArray(toUInt32(splitByChar('#', t.source_id)[2])) AS targets
FROM (SELECT id, source_id, created_at, is_crash_ci FROM {db}.github_issues FINAL
      WHERE is_private = 0 AND state_reason = 'duplicate'
        AND splitByChar('#', source_id)[1] = {{repo:String}}) AS d
JOIN {db}.edges AS e ON (e.from_id = d.id OR e.to_id = d.id) AND e.rel_type = 'related_to'
JOIN (SELECT id, source_id, created_at FROM {db}.github_issues FINAL WHERE is_private = 0) AS t
  ON t.id = if(e.from_id = d.id, e.to_id, e.from_id)
WHERE t.created_at < d.created_at
GROUP BY dup, dup_number, dup_created, crash
ORDER BY dup_created DESC
LIMIT {{limit:UInt32}}
"""


def load_env(path: str) -> None:
    """The named .env, line by line (JSON values intact), never `source`d."""
    if not os.path.exists(path):
        raise SystemExit(f"--env-file {path} does not exist")
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] not in "{\"'" and " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        if v[:1] in "\"'" and v[-1:] == v[:1]:
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


async def gold(
    repo: str,
    limit: int,
    *,
    namespace: str,
    stratum: str = "duplicate",
) -> list[dict[str, Any]]:
    import server.tracker.store.source.qa_intel as qi
    from server.settings import settings
    from server.tracker.domain.namespaces import tracker_namespace

    cfg = tracker_namespace(settings.tracker_org, namespace)
    if cfg is None:
        raise SystemExit(f"{namespace} is not a configured tracker namespace")
    sql = GOLD_SQL if stratum == "duplicate" else RELATED_SQL
    rows = await qi.query_rows(
        sql.format(db=cfg.database),
        {"repo": repo, "limit": limit},
        credential=cfg.credential,
        max_execution_s=20.0,
    )
    return [
        {
            "number": int(r["dup_number"]),
            "created_at": str(r["dup_created"]),
            "crash": int(r.get("crash") or 0),
            "targets": sorted({int(x) for x in r["targets"]}),
        }
        for r in rows
    ]


def gold_from_file(path: str) -> list[dict[str, Any]]:
    """The recorded duplicate pairs (`{pairs: [{number, targets, crash}]}`)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pairs = data["pairs"] if isinstance(data, dict) else data
    return [
        {
            "number": int(p["number"]),
            "created_at": "",
            "crash": int(p.get("crash") or 0),
            "targets": sorted(int(t) for t in p["targets"]),
        }
        for p in pairs
    ]


def cites(mentions: list[int], targets: list[int]) -> bool:
    """Whether the op's own `query.mentions` — the numbers the item's title
    and body head name (`#N`, `/issues/N`, `/pull/N`, the first ten; what
    the mentions leg reads) — include a target. The named / not-named split ("where only meaning can
    find the target") is this rule."""
    return any(int(t) in set(mentions or []) for t in targets)


async def call(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    namespace: str,
    number: int,
    arm: dict[str, Any],
    org: str = "dev",
) -> tuple[dict[str, Any], float]:
    body: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        "issue_number": number,
        "kinds": arm["kinds"],
        "top_k": int(arm.get("top_k") or 10),
    }
    if arm["legs"]:
        body["legs"] = arm["legs"]
    # the `default` rate class is 50 burst / 10 per second (server/core/ratelimit.py):
    # cached arms answer in ~10 ms, so the bench paces itself and backs off on 429
    for attempt in range(6):
        t0 = time.perf_counter()
        r = await client.post(
            f"{base}/v1/tracker.similar",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        wall = (time.perf_counter() - t0) * 1000.0
        if r.status_code == 429:
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        r.raise_for_status()
        await asyncio.sleep(0.12)
        return r.json(), wall
    raise RuntimeError("rate limited six times in a row")


def _rank(items: list[dict[str, Any]], targets: list[int]) -> int:
    for i, it in enumerate(items, start=1):
        if it.get("kind") == "issue" and int(it.get("number") or 0) in targets:
            return i
    return 0


def summarise(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for arm in dict.fromkeys(r["arm"] for r in rows):
        per = [r for r in rows if r["arm"] == arm and not r.get("tracker_unavailable")]
        n = len(per)
        if not n:
            continue
        ranks = [r["rank"] for r in per]
        walls = sorted(r["wall_ms"] for r in per)
        out[arm] = {
            "n": n,
            "hit@1": sum(1 for k in ranks if k == 1) / n,
            "hit@5": sum(1 for k in ranks if 0 < k <= 5) / n,
            "hit@10": sum(1 for k in ranks if 0 < k <= 10) / n,
            "mrr@10": sum(1.0 / k for k in ranks if 0 < k <= 10) / n,
            "wall_p50_ms": statistics.median(walls),
            "wall_p90_ms": walls[min(len(walls) - 1, int(0.9 * len(walls)))],
            "wall_p95_ms": walls[min(len(walls) - 1, int(0.95 * len(walls)))],
            "unavailable": sum(1 for r in rows if r["arm"] == arm and r.get("tracker_unavailable")),
            # What the vector leg said on this arm
            "vector_served": sum(1 for r in per if (r.get("legs") or {}).get("vector") == "served"),
        }
    return out


def print_table(title: str, summary: dict[str, dict[str, float]]) -> None:
    print(title)
    print(
        f"{'arm':<24}{'n':>5}{'hit@1':>8}{'hit@5':>8}{'hit@10':>8}{'mrr@10':>8}"
        f"{'p50 ms':>9}{'p90 ms':>9}{'p95 ms':>9}{'unav':>6}{'vec':>5}"
    )
    for arm, s in summary.items():
        print(
            f"{arm:<24}{s['n']:>5}{s['hit@1']:>8.3f}{s['hit@5']:>8.3f}{s['hit@10']:>8.3f}"
            f"{s['mrr@10']:>8.3f}{s['wall_p50_ms']:>9.0f}{s['wall_p90_ms']:>9.0f}"
            f"{s['wall_p95_ms']:>9.0f}{s['unavailable']:>6}{s['vector_served']:>5}"
        )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:7953")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--env-file", default="", help="required unless --pairs-file")
    ap.add_argument("--org", default="dev", help="org of the tracker.similar calls")
    ap.add_argument("--namespace", default="tracker-clickhouse")
    ap.add_argument("--repo", default="ClickHouse/ClickHouse")
    ap.add_argument("--limit", type=int, default=500)
    # the FIRST arm per query pays the uncached item and lexical reads: order them
    ap.add_argument("--arms", nargs="*", choices=list(ARMS), default=list(ARMS))
    ap.add_argument("--out", default="")
    # A recorded pairs file replays the gold exactly; `--gold related` is the secondary stratum
    ap.add_argument("--pairs-file", default="")
    ap.add_argument("--gold", choices=["duplicate", "related"], default="duplicate")
    # a second (third…) pass over the same queries: the production-like WARM walls
    # (the item, vocabulary and lexical reads cached), labelled `arm [warm N]`
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    if not args.pairs_file and not args.env_file:
        ap.error("--env-file is required to read the gold (or pass --pairs-file)")
    if args.env_file:
        load_env(args.env_file)
    for k in list(os.environ):
        if k.startswith("LOOM_OTEL") or k.startswith("OTEL_"):
            os.environ.pop(k)
    os.environ.setdefault("LOOM_BACKGROUND_WORKERS", "off")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if args.pairs_file:
        queries = gold_from_file(args.pairs_file)
    else:
        queries = await gold(args.repo, args.limit, namespace=args.namespace, stratum=args.gold)
    print(
        f"gold ({args.gold}): {len(queries)} issues with an older related_to issue "
        f"({sum(1 for q in queries if q['crash'])} crash-CI)",
        file=sys.stderr,
    )
    if not queries:
        print("no gold queries: nothing to measure", file=sys.stderr)
        return 1
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        # one warm call: the vocabulary read (~300 ms) and the connection are paid once
        first, cold = await call(
            client,
            args.base_url,
            token,
            args.namespace,
            queries[-1]["number"],
            ARMS["lexical"],
            args.org,
        )
        print(f"cold call: {cold:.0f} ms legs={first.get('legs')}", file=sys.stderr)
        passes = [(n, i, q) for n in range(max(1, args.repeat)) for i, q in enumerate(queries)]
        for rep_n, i, q in passes:
            for arm in args.arms:
                out, wall = await call(
                    client, args.base_url, token, args.namespace, q["number"], ARMS[arm], args.org
                )
                items = out.get("items") or []
                if "cites" not in q:
                    mentions = (out.get("query") or {}).get("mentions") or []
                    q["cites"] = int(cites(mentions, q["targets"]))
                label = arm if rep_n == 0 else f"{arm} [warm {rep_n}]"
                rows.append(
                    {
                        "arm": label,
                        "number": q["number"],
                        "targets": q["targets"],
                        "crash": q["crash"],
                        "cites": q["cites"],
                        "rank": _rank(items, q["targets"]),
                        "wall_ms": round(wall, 1),
                        "tracker_unavailable": bool(out.get("tracker_unavailable")),
                        "legs": out.get("legs"),
                        "terms": (out.get("query") or {}).get("terms"),
                        "frames": (out.get("query") or {}).get("frames"),
                        "top": [(it.get("number"), it.get("why")) for it in items[:5]],
                        "fresh_items": (out.get("query") or {}).get("fresh_items"),
                        "timings_ms": (out.get("query") or {}).get("timings_ms"),
                    }
                )
            if (i + 1) % 20 == 0:
                print(f"  pass {rep_n + 1}: {i + 1}/{len(queries)}", file=sys.stderr)
    print(
        f"{sum(1 for q in queries if q['cites'])} of {len(queries)} name a target in the body "
        f"(the op's query.mentions)",
        file=sys.stderr,
    )
    summary = summarise(rows)
    print_table(f"all ({len(queries)} queries)", summary)
    uncited = [r for r in rows if not r["cites"]]
    uncited_summary = summarise(uncited)
    if uncited:
        print_table(
            f"target NOT named in the body ({len({r['number'] for r in uncited})} queries)",
            uncited_summary,
        )
    crash = [r for r in rows if r["crash"]]
    if crash:
        print(f"crash-CI slice: {len({r['number'] for r in crash})} queries")
        for arm, s in summarise(crash).items():
            print(f"  {arm:<22}{s['n']:>5}{s['hit@5']:>8.3f}{s['hit@10']:>8.3f}{s['mrr@10']:>8.3f}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"summary": summary, "uncited": uncited_summary, "rows": rows},
                indent=1,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
