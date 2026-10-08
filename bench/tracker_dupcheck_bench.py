"""tracker.duplicate_check as a reporter meets it: is my draft already filed?

Gold: the repository's public issues closed as duplicates whose `related_to` edge
names an OLDER issue (the same set bench/tracker_similar_bench.py reads). Each
duplicate is replayed as a new draft — its own title and body, `as_of` its
creation time, and NO `issue_number`, so the op sees neither the item itself nor
the graph edge that records the answer. A hit is the older issue among the
returned items.

Negatives: issues of the same repository that were never closed as duplicates
and carry no `related_to` / `duplicate_of` edge, replayed the same way. A
`duplicate_of` verdict on one of them would tell a reporter not to file a new
problem; the false-duplicate rate is that share.

Reported, per arm:
* hit@1 / hit@5 / hit@10 / MRR@10 of the older issue on the duplicates;
* the verdict mix on duplicates (duplicate_of / related / none) and how often
  `duplicate_of` names the right issue first (precision of the verdict);
* the false-duplicate rate on the negatives;
* the op's wall p50 / p95 (client side, one call at a time).

Titles and bodies are read from the tracker store (public issues only) with the
env file's tracker credential; the op calls go over HTTP. Nothing is written anywhere.

    python -m bench.tracker_dupcheck_bench --base-url http://127.0.0.1:7961 \\
        --token-file out/tracker.token --env-file /path/to/.env \\
        --limit 200 --negatives 100 --out out/dupcheck.json
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

from bench.tracker_similar_bench import gold

NEGATIVES_SQL = """
SELECT toUInt32(splitByChar('#', source_id)[2]) AS number, created_at
FROM {db}.github_issues FINAL
WHERE is_private = 0 AND state_reason != 'duplicate'
  AND splitByChar('#', source_id)[1] = {{repo:String}}
  AND created_at >= {{since:String}}
  AND id NOT IN (SELECT from_id FROM {db}.edges WHERE rel_type IN ('related_to', 'duplicate_of'))
  AND id NOT IN (SELECT to_id FROM {db}.edges WHERE rel_type IN ('related_to', 'duplicate_of'))
ORDER BY cityHash64(id)
LIMIT {{limit:UInt32}}
"""


async def negatives(repo: str, limit: int, since: str, *, namespace: str) -> list[dict[str, Any]]:
    import server.tracker.store.source.qa_intel as qi
    from server.settings import settings
    from server.tracker.domain.namespaces import tracker_namespace

    cfg = tracker_namespace(settings.tracker_org, namespace)
    if cfg is None:
        raise SystemExit(f"{namespace} is not a configured tracker namespace")
    rows = await qi.query_rows(
        NEGATIVES_SQL.format(db=cfg.database),
        {"repo": repo, "limit": limit, "since": since},
        credential=cfg.credential,
        max_execution_s=20.0,
    )
    return [
        {"number": int(r["number"]), "created_at": str(r["created_at"]), "targets": []}
        for r in rows
    ]


async def _post(
    client: httpx.AsyncClient, url: str, token: str, body: dict[str, Any]
) -> tuple[dict[str, Any], float]:
    for attempt in range(6):
        t0 = time.perf_counter()
        r = await client.post(
            url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60.0
        )
        wall = (time.perf_counter() - t0) * 1000.0
        if r.status_code == 429:
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        r.raise_for_status()
        return r.json(), wall
    raise RuntimeError("rate limited six times in a row")


TEXTS_SQL = """
SELECT toUInt32(splitByChar('#', source_id)[2]) AS number, title, body
FROM {db}.github_issues FINAL
WHERE is_private = 0 AND splitByChar('#', source_id)[1] = {{repo:String}}
  AND toUInt32(splitByChar('#', source_id)[2]) IN {{numbers:Array(UInt32)}}
"""


async def drafts(repo: str, numbers: list[int], *, namespace: str) -> dict[int, dict[str, Any]]:
    """number -> {title, body}: the text the reporter filed, from the tracker store."""
    import server.tracker.store.source.qa_intel as qi
    from server.settings import settings
    from server.tracker.domain.namespaces import tracker_namespace

    cfg = tracker_namespace(settings.tracker_org, namespace)
    if cfg is None:
        raise SystemExit(f"{namespace} is not a configured tracker namespace")
    out: dict[int, dict[str, Any]] = {}
    for i in range(0, len(numbers), 200):
        rows = await qi.query_rows(
            TEXTS_SQL.format(db=cfg.database),
            {"repo": repo, "numbers": numbers[i : i + 200]},
            credential=cfg.credential,
            max_execution_s=20.0,
        )
        for r in rows:
            out[int(r["number"])] = {"title": str(r["title"] or ""), "body": str(r["body"] or "")}
    return out


def as_of(created_at: str) -> str:
    """The QA store's `YYYY-MM-DD hh:mm:ss` as ISO 8601 UTC."""
    s = created_at.strip().replace(" ", "T")
    return s if s.endswith("Z") or "+" in s[10:] else s + "Z"


def rank_of(items: list[dict[str, Any]], targets: list[int]) -> int:
    for i, it in enumerate(items, start=1):
        if it.get("kind", "issue") == "issue" and int(it.get("number") or 0) in targets:
            return i
    return 0


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pos = [r for r in rows if r["targets"] and not r.get("unavailable")]
    neg = [r for r in rows if not r["targets"] and not r.get("unavailable")]
    walls = sorted(r["wall_ms"] for r in rows if not r.get("unavailable"))
    n = len(pos)
    dup_verdicts = [r for r in pos if r["verdict"] == "duplicate_of"]
    return {
        "duplicates": n,
        "hit@1": round(sum(1 for r in pos if r["rank"] == 1) / max(1, n), 4),
        "hit@5": round(sum(1 for r in pos if 0 < r["rank"] <= 5) / max(1, n), 4),
        "hit@10": round(sum(1 for r in pos if 0 < r["rank"] <= 10) / max(1, n), 4),
        "mrr@10": round(sum(1.0 / r["rank"] for r in pos if 0 < r["rank"] <= 10) / max(1, n), 4),
        "verdicts_on_duplicates": {
            v: sum(1 for r in pos if r["verdict"] == v) for v in ("duplicate_of", "related", "none")
        },
        "duplicate_verdict_precision": round(
            sum(1 for r in dup_verdicts if r["rank"] == 1) / max(1, len(dup_verdicts)), 4
        ),
        "negatives": len(neg),
        "false_duplicate_rate": round(
            sum(1 for r in neg if r["verdict"] == "duplicate_of") / max(1, len(neg)), 4
        ),
        "negatives_verdicts": {
            v: sum(1 for r in neg if r["verdict"] == v) for v in ("duplicate_of", "related", "none")
        },
        "unavailable": sum(1 for r in rows if r.get("unavailable")),
        "wall_p50_ms": round(statistics.median(walls)) if walls else 0,
        "wall_p95_ms": round(walls[min(len(walls) - 1, int(0.95 * len(walls)))]) if walls else 0,
    }


async def run(args: argparse.Namespace) -> int:
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    pos = await gold(args.repo, args.limit, namespace=args.namespace)
    since = min((q["created_at"] for q in pos), default="2000-01-01 00:00:00")
    neg = await negatives(args.repo, args.negatives, since, namespace=args.namespace)
    print(f"gold: {len(pos)} duplicates, {len(neg)} negatives", file=sys.stderr)
    queries = pos + neg
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        texts = await drafts(args.repo, [q["number"] for q in queries], namespace=args.namespace)
        for q in queries:
            d = texts.get(q["number"])
            if not d or not (d["title"] or d["body"]):
                continue
            out, wall = await _post(
                client,
                f"{args.base_url}/v1/tracker.duplicate_check",
                token,
                {
                    "org": args.org,
                    "namespace": args.namespace,
                    "title": d["title"][:500],
                    "body": d["body"][:20000],
                    "as_of": as_of(q["created_at"]),
                    "top_k": 10,
                },
            )
            items = out.get("items") or []
            rows.append(
                {
                    "number": q["number"],
                    "targets": q["targets"],
                    "verdict": str(out.get("verdict") or "none"),
                    "rank": rank_of(items, q["targets"]) if q["targets"] else 0,
                    "top": [int(it.get("number") or 0) for it in items[:10]],
                    "unavailable": bool(out.get("tracker_unavailable")),
                    "wall_ms": round(wall),
                }
            )
            await asyncio.sleep(0.12)
    summary = summarise(rows)
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--env-file", required=True, help="env whose tracker credential reads the gold")
    ap.add_argument("--org", default="clickhouse")
    ap.add_argument("--namespace", default="qa-clickhouse")
    ap.add_argument("--repo", default="ClickHouse/ClickHouse")
    ap.add_argument("--limit", type=int, default=200, help="duplicates replayed")
    ap.add_argument("--negatives", type=int, default=100, help="never-duplicate issues replayed")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    # Settings read the file as the server does (JSON values intact).
    os.environ["LOOM_ENV_FILE"] = args.env_file
    for k in list(os.environ):
        if k.startswith(("LOOM_OTEL", "OTEL_")):
            os.environ.pop(k)
    os.environ.setdefault("LOOM_BACKGROUND_WORKERS", "off")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
