"""Document retrieval quality of `knowledge.search` on a small labelled corpus.

The fixture (`bench/fixtures/knowledge_retrieval/`) is synthetic: handbooks of
four fictional products that share one heading layout, runbooks keyed by error
code, an old and a current version of one policy, design notes and meeting
notes. Each query names its relevant passages as (source_uri, answer text)
pairs: a hit is relevant when it comes from that document and its content holds
that text, so the labels survive a chunker change that keeps each answer inside
one chunk. Queries the corpus cannot answer carry no gold and measure how
similar the top hit still looks.

Two phases, run together by default into a fresh namespace that is erased when
the run ends. To compare arms on one ingest, ingest once into a namespace you
name, which is kept, and search it per arm:

    PYTHONPATH=$PWD .venv/bin/python bench/knowledge_retrieval.py \\
        --base-url http://localhost:7790 --namespace kr-eval --phase ingest
    PYTHONPATH=$PWD .venv/bin/python bench/knowledge_retrieval.py \\
        --base-url http://localhost:7790 --namespace kr-eval --phase search \\
        --param search_mode=hybrid --out /tmp/kr.json

A named namespace is ingested into only when it is unregistered or a bench
namespace, and stays until it is erased:
`loom namespaces-gc --org bench --include-registered --erase --namespace kr-eval`.

`--param KEY=VALUE` (JSON value, or a plain string) sets any `knowledge.search`
field but the ones the harness sets (`FIXED_FIELDS`), so two arms differ by one
flag; any other key is refused, and a search over a namespace that does not hold
exactly the fixture's documents fails the run. Reports recall@1/5/10 (a query
counts when any relevant passage is in the top k), all-gold recall@10 (every
relevant passage found, for the multi-document questions), MRR and nDCG@10,
overall and per query kind, plus the mean cosine similarity of the top hit on
answerable and on unanswerable queries (every hit carries one, in every mode).
The `freshness` and `multi` kinds hold three and four queries, so their per-kind
figures are coarse; a superseded policy ranked above the current one shows in a
freshness query's R@1, MRR and nDCG@10, not its R@5. Ingesting embeds 21
documents (97 chunks at the default chunk size): a few cents with a hosted
embedder, free with a local one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from bench._http import post_json
from bench.bench_namespaces import (
    BENCH_OWNER_EMAIL,
    SyncPost,
    erase_on_exit_sync,
    register_bench_namespace_sync,
    remember_bench_namespace,
)
from server.knowledge.api.models_knowledge import KnowledgeSearchInput

FIXTURE = Path(__file__).parent / "fixtures" / "knowledge_retrieval"
KS = (1, 5, 10)
TOP_K = max(KS)


def fixture_tag(docs: list[dict]) -> str:
    """A tag naming this version of the corpus, put on every ingested document so a
    search can tell an ingest of an older corpus from this one."""
    digest = hashlib.sha256(json.dumps(docs, sort_keys=True).encode()).hexdigest()
    return f"fixture:{digest[:12]}"


def load_fixture(root: Path) -> tuple[list[dict], list[dict]]:
    docs = json.loads((root / "corpus.json").read_text())
    lines = (root / "queries.jsonl").read_text().splitlines()
    queries = [json.loads(line) for line in lines if line and not line.startswith("//")]
    return docs, queries


def relevant(hit: dict, gold: list[list[str]]) -> list[int]:
    """The indexes of the gold passages `hit` holds."""
    content = hit.get("content") or ""
    return [
        i for i, (uri, text) in enumerate(gold) if hit.get("source_uri") == uri and text in content
    ]


def score_query(hits: list[dict], gold: list[list[str]]) -> dict[str, float]:
    """Binary-relevance metrics for one query. A gold passage counts once, at its best rank."""
    found: dict[int, int] = {}
    for rank, hit in enumerate(hits, start=1):
        for g in relevant(hit, gold):
            found.setdefault(g, rank)
    first = min(found.values(), default=None)
    out = {f"recall@{k}": float(first is not None and first <= k) for k in KS}
    # `hits` is the top TOP_K, so every found passage is within it.
    out[f"all_gold@{TOP_K}"] = float(len(found) == len(gold))
    out["mrr"] = 1.0 / first if first else 0.0
    dcg = sum(1.0 / math.log2(r + 1) for r in found.values())
    ideal = sum(1.0 / math.log2(r + 1) for r in range(1, min(len(gold), TOP_K) + 1))
    # Capped: one chunk holding two gold passages credits both at its rank.
    out[f"ndcg@{TOP_K}"] = min(1.0, dcg / ideal) if ideal else 0.0
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


# Fields that define the measurement: the depth the metrics are computed at, and what
# is searched. Anything else that is not a search field is refused too.
FIXED_FIELDS = ("top_k", "org", "namespace", "namespaces", "namespace_scope", "query")
SEARCH_FIELDS = frozenset(KnowledgeSearchInput.model_fields) - set(FIXED_FIELDS)


def parse_params(pairs: list[str]) -> dict:
    """`KEY=VALUE` pairs as search fields; a key outside SEARCH_FIELDS is refused."""
    out = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if key in FIXED_FIELDS:
            raise SystemExit(f"--param {key} is refused: the harness sets it")
        if key not in SEARCH_FIELDS:
            raise SystemExit(f"--param {key} is refused: not a knowledge.search field")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def run_namespace(named: str | None, keep_flag: bool) -> tuple[str, bool]:
    """(the namespace a run uses, whether it outlives the run). Only a namespace the
    harness made up is erased: one the caller named may hold more than the fixture."""
    if named:
        return named, True
    return f"knowledge-retrieval-{secrets.token_hex(4)}", keep_flag


def may_ingest_into(info: dict) -> bool:
    """Whether `namespace.get`'s answer leaves the namespace to the harness: never
    registered, or registered as a bench namespace (registering rewrites its owner)."""
    return not info.get("found") or info.get("owner_email") == BENCH_OWNER_EMAIL


def ingest(post: SyncPost, org: str, namespace: str, docs: list[dict], keep: bool) -> None:
    info = post("/v1/namespace.get", {"org": org, "namespace": namespace})
    if not may_ingest_into(info):
        raise SystemExit(f"{org}/{namespace} belongs to {info.get('owner_email')}: name another")
    fixture = {d["source_uri"] for d in docs}
    foreign = [r["source_uri"] for r in listed_documents(post, org, namespace)]
    foreign = [uri for uri in foreign if uri not in fixture]
    if foreign:
        raise SystemExit(f"{org}/{namespace} holds {len(foreign)} other documents: name another")
    register_bench_namespace_sync(
        post, org=org, namespace=namespace, description="knowledge retrieval fixture", keep=keep
    )
    remember_bench_namespace(org, namespace)
    tag = fixture_tag(docs)
    for d in docs:
        body = {"org": org, "namespace": namespace, **d, "tags": [*d["tags"], tag]}
        post("/v1/knowledge.upsert", body)
    print(f"ingested {len(docs)} documents into {org}/{namespace}", file=sys.stderr)


def listed_documents(post: SyncPost, org: str, namespace: str) -> list[dict]:
    """Every live document of the namespace (`knowledge.list`, all pages)."""
    rows: list[dict] = []
    cursor = None
    while True:
        page = post(
            "/v1/knowledge.list",
            {"org": org, "namespace": namespace, "limit": 500, "cursor": cursor},
        )
        rows.extend(page["rows"])
        cursor = page.get("next_cursor")
        if not cursor:
            return rows


def check_corpus(post: SyncPost, org: str, namespace: str, docs: list[dict]) -> None:
    """Refuse a namespace whose documents are not exactly this fixture's: an older or
    partial ingest skews every number without failing a query."""
    tag = fixture_tag(docs)
    rows = listed_documents(post, org, namespace)
    have = {row["source_uri"] for row in rows}
    stale = sum(tag not in (row.get("tags") or []) for row in rows)
    want = {d["source_uri"] for d in docs}
    if have != want or stale:
        raise SystemExit(
            f"{org}/{namespace} is not this fixture: {len(want - have)} documents missing, "
            f"{len(have - want)} extra, {stale} from another version; "
            "ingest into a fresh namespace"
        )


def search(post: SyncPost, org: str, namespace: str, queries: list[dict], params: dict) -> dict:
    rows, top_answerable, top_unanswerable = [], [], []
    any_hits = False
    for q in queries:
        body = {
            **params,
            "org": org,
            "namespace": namespace,
            "namespace_scope": "this",
            "query": q["query"],
            "top_k": TOP_K,
        }
        out = post("/v1/knowledge.search", body)
        if out.get("ignored_args"):
            raise SystemExit(f"knowledge.search ignored {out['ignored_args']}: the arm did not run")
        hits = out["results"]
        top = hits[0]["similarity"] if hits else 0.0
        if not q["gold"]:
            top_unanswerable.append(top)
            continue
        top_answerable.append(top)
        any_hits = any_hits or bool(hits)
        rows.append({"id": q["id"], "kind": q["kind"], "metrics": score_query(hits, q["gold"])})
    if not any_hits:
        raise SystemExit(f"no query found anything in {org}/{namespace}: was it ingested?")
    return {
        "params": params,
        "metrics": mean_by_kind(rows),
        # The gap between the two is the room a similarity floor has to abstain.
        "top_similarity": {
            "answerable": round(sum(top_answerable) / max(1, len(top_answerable)), 3),
            "unanswerable": round(sum(top_unanswerable) / max(1, len(top_unanswerable)), 3),
        },
        "misses@5": [r["id"] for r in rows if not r["metrics"]["recall@5"]],
        "queries": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="bench")
    ap.add_argument(
        "--namespace",
        help="the namespace to ingest into or search, never erased "
        "(default: a fresh one per run, erased at its end)",
    )
    ap.add_argument("--fixture", type=Path, default=FIXTURE)
    ap.add_argument("--phase", choices=("ingest", "search", "both"), default="both")
    ap.add_argument("--param", action="append", default=[], help="knowledge.search KEY=VALUE")
    ap.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="keep the fresh namespace a run ingests into",
    )
    ap.add_argument("--out", type=Path, help="write the full report (per query) as JSON")
    args = ap.parse_args()
    if args.phase != "both" and not args.namespace:
        ap.error(f"--phase {args.phase} needs a --namespace (one ingest, many searches)")

    params = parse_params(args.param)
    namespace, keep = run_namespace(args.namespace, args.keep_namespaces)
    docs, queries = load_fixture(args.fixture)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    base = args.base_url.rstrip("/")
    with httpx.Client(headers=headers) as client:

        def post(path: str, body: dict[str, Any]) -> Any:
            return post_json(client, f"{base}{path}", body, attempts=3, timeout_s=120.0)

        with erase_on_exit_sync(post, org=args.org, keep=keep):
            if args.phase in ("ingest", "both"):
                ingest(post, args.org, namespace, docs, keep)
            if args.phase == "ingest":
                return 0
            check_corpus(post, args.org, namespace, docs)
            report = search(post, args.org, namespace, queries, params)
    if args.out:
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    summary = {k: v for k, v in report.items() if k != "queries"}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
