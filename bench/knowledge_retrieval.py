"""Document retrieval quality of `knowledge.search` on a small labelled corpus.

The fixture (`bench/fixtures/knowledge_retrieval/`) is synthetic: handbooks of
four fictional products that share one heading layout, runbooks keyed by error
code, an old and a current version of one policy, design notes and meeting
notes. Each query names its relevant passages as (source_uri, answer text)
pairs, so the labels survive a change of chunk size or chunker: a hit is
relevant when it comes from that document and its content holds that text.
Queries the corpus cannot answer carry no gold and measure how confident the
top hit still looks.

Two phases, run together by default; a run that ingests erases its namespace at
the end unless `--keep-namespace` (implied by `--phase ingest`) keeps it for
later search arms:

    PYTHONPATH=$PWD .venv/bin/python bench/knowledge_retrieval.py \\
        --base-url http://localhost:7790 --namespace kr-eval --phase ingest
    PYTHONPATH=$PWD .venv/bin/python bench/knowledge_retrieval.py \\
        --base-url http://localhost:7790 --namespace kr-eval --phase search \\
        --param search_mode=hybrid --out /tmp/kr.json

`--param KEY=VALUE` (JSON value, or a plain string) sets any `knowledge.search`
field, so two arms differ by one flag. Reports recall@1/5/10 (a query counts
when any relevant passage is in the top k), all-gold recall@10 (every relevant
passage found, for the multi-document questions), MRR and nDCG@10, overall and
per query kind, plus the mean top-hit score on unanswerable queries.
Ingesting embeds 21 documents (about 130 chunks): a few cents with a hosted
embedder, free with a local one.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import httpx

from bench._http import post_json
from bench.bench_namespaces import (
    erase_on_exit_sync,
    register_bench_namespace_sync,
    remember_bench_namespace,
)

FIXTURE = Path(__file__).parent / "fixtures" / "knowledge_retrieval"
KS = (1, 5, 10)


def load_fixture(root: Path) -> tuple[list[dict], list[dict]]:
    docs = json.loads((root / "corpus.json").read_text())
    queries = [json.loads(line) for line in (root / "queries.jsonl").read_text().splitlines()]
    return docs, queries


def relevant(hit: dict, gold: list[list[str]]) -> int | None:
    """The index of the gold passage `hit` holds, or None."""
    for i, (uri, text) in enumerate(gold):
        if hit.get("source_uri") == uri and text in (hit.get("content") or ""):
            return i
    return None


def score_query(hits: list[dict], gold: list[list[str]]) -> dict[str, float]:
    """Binary-relevance metrics for one query. A gold passage counts once, at its best rank."""
    found: dict[int, int] = {}
    for rank, hit in enumerate(hits, start=1):
        g = relevant(hit, gold)
        if g is not None and g not in found:
            found[g] = rank
    first = min(found.values(), default=None)
    out = {f"recall@{k}": float(first is not None and first <= k) for k in KS}
    out["all_gold@10"] = float(len([r for r in found.values() if r <= 10]) == len(gold))
    out["mrr"] = 1.0 / first if first else 0.0
    dcg = sum(1.0 / math.log2(r + 1) for r in found.values() if r <= 10)
    ideal = sum(1.0 / math.log2(r + 1) for r in range(1, min(len(gold), 10) + 1))
    out["ndcg@10"] = dcg / ideal if ideal else 0.0
    return out


def mean_by_kind(rows: list[dict]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups["all"].append(r)
        groups[r["kind"]].append(r)
    out = {}
    for kind, items in sorted(groups.items()):
        keys = items[0]["metrics"].keys()
        out[kind] = {k: round(sum(i["metrics"][k] for i in items) / len(items), 3) for k in keys}
        out[kind]["n"] = len(items)
    return out


def parse_params(pairs: list[str]) -> dict:
    out = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def ingest(
    client: httpx.Client, base: str, org: str, namespace: str, docs: list[dict], keep: bool
) -> None:
    def post(path: str, body: dict) -> dict:
        return post_json(client, f"{base}{path}", body, attempts=3)

    register_bench_namespace_sync(
        post, org=org, namespace=namespace, description="knowledge retrieval fixture", keep=keep
    )
    remember_bench_namespace(org, namespace)
    for d in docs:
        body = {"org": org, "namespace": namespace, **d}
        post_json(client, f"{base}/v1/knowledge.upsert", body, attempts=3)
    print(f"ingested {len(docs)} documents into {org}/{namespace}", file=sys.stderr)


def search(
    client: httpx.Client, base: str, org: str, namespace: str, queries: list[dict], params: dict
) -> dict:
    rows, unanswerable = [], []
    for q in queries:
        body = {
            "org": org,
            "namespace": namespace,
            "namespace_scope": "this",
            "query": q["query"],
            "top_k": 10,
            **params,
        }
        hits = post_json(client, f"{base}/v1/knowledge.search", body, attempts=3)["results"]
        if not q["gold"]:
            unanswerable.append(hits[0]["score"] if hits else 0.0)
            continue
        m = score_query(hits, q["gold"])
        rows.append({"id": q["id"], "kind": q["kind"], "metrics": m})
    return {
        "params": params,
        "metrics": mean_by_kind(rows),
        "unanswerable_top_score": round(sum(unanswerable) / max(1, len(unanswerable)), 3),
        "misses@5": [r["id"] for r in rows if not r["metrics"]["recall@5"]],
        "queries": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="bench")
    ap.add_argument("--namespace", default="knowledge-retrieval")
    ap.add_argument("--fixture", type=Path, default=FIXTURE)
    ap.add_argument("--phase", choices=("ingest", "search", "both"), default="both")
    ap.add_argument("--param", action="append", default=[], help="knowledge.search KEY=VALUE")
    ap.add_argument(
        "--keep-namespace",
        action="store_true",
        help="keep the ingested namespace for later --phase search runs (implied by --phase ingest)",
    )
    ap.add_argument("--out", type=Path, help="write the full report (per query) as JSON")
    args = ap.parse_args()

    docs, queries = load_fixture(args.fixture)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    base = args.base_url.rstrip("/")
    keep = args.keep_namespace or args.phase == "ingest"
    with httpx.Client(timeout=120, headers=headers) as client:

        def post(path: str, body: dict) -> dict:
            return post_json(client, f"{base}{path}", body, attempts=3)

        with erase_on_exit_sync(post, org=args.org, keep=keep):
            if args.phase in ("ingest", "both"):
                ingest(client, base, args.org, args.namespace, docs, keep)
            if args.phase == "ingest":
                return 0
            params = parse_params(args.param)
            report = search(client, base, args.org, args.namespace, queries, params)
    if args.out:
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    summary = {k: v for k, v in report.items() if k != "queries"}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
