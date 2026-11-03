"""BEIR passage retrieval through `knowledge.search`: nDCG@10, recall@10/100, MRR@10.

A BEIR dataset is a corpus of passages, a set of queries and graded relevance
judgements (qrels). Each passage is ingested as one document (`beir:<doc id>`,
its title and text), each test query with a judgement is searched, and the hits
are scored against the qrels by document, the passage's chunks counted once at
the rank of the first. The small datasets (`SMALL`) run in minutes; `--fetch`
downloads one from the BEIR release and keeps only its corpus, queries and test
qrels:

    PYTHONPATH=$PWD .venv/bin/python bench/beir_knowledge.py --fetch scifact --data-dir /tmp/beir

Then ingest once into a namespace you name, which is kept, and search it per arm:

    PYTHONPATH=$PWD .venv/bin/python bench/beir_knowledge.py --data /tmp/beir/scifact \\
        --base-url http://localhost:7790 --namespace beir-scifact --phase ingest
    PYTHONPATH=$PWD .venv/bin/python bench/beir_knowledge.py --data /tmp/beir/scifact \\
        --base-url http://localhost:7790 --namespace beir-scifact --phase search \\
        --param search_mode=hybrid --out /tmp/beir-hybrid.json

Without `--namespace` both phases run into a fresh namespace erased at the end.
A named namespace must start with `beir-` (and not `beir-run-`, a run's own), as
`knowledge_retrieval.py` does with its prefix, and a search over a namespace that
does not hold exactly this corpus fails the run. `--param KEY=VALUE` sets any
`knowledge.search` field the harness does not fix (`FIXED_FIELDS`). A run records
the server's embedding model and build and refuses a fake embedder.

Ingest cost is the corpus's embedding tokens (SciFact ≈ 5K passages, NFCorpus ≈
3.6K, FiQA ≈ 57K, SCIDOCS ≈ 25K); a search arm embeds only its queries, plus one
model call per query when it reranks. Two arms pair by query id
(`bench.bootstrap.paired_bootstrap`, each query its own cluster).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import secrets
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx

from bench._http import post_json
from bench.bench_namespaces import (
    SyncPost,
    erase_on_exit_sync,
    gc_command,
    register_bench_namespace_sync,
    remember_bench_namespace,
)
from bench.knowledge_retrieval import listed_documents, parse_params, require_real_embedder
from bench.quality import run_metadata

RELEASE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{name}.zip"
SMALL = ("scifact", "nfcorpus", "fiqa", "scidocs", "arguana", "trec-covid")
# What a fetch keeps from the release archive: nothing else is written.
KEPT = ("corpus.jsonl", "queries.jsonl", "qrels/test.tsv")
PREFIX = "beir-"
FRESH_PREFIX = f"{PREFIX}run-"
URI_PREFIX = "beir:"
# Hits a search asks for: the wire bound, so recall@100 counts every hit it can.
DEPTH = 100
BATCH = 200  # documents per knowledge.bulk_add; the op takes at most 500


def fetch(name: str, data_dir: Path) -> Path:
    """Download one BEIR dataset and write its corpus, queries and test qrels under
    `data_dir/name`. Archive members are read by name, never extracted as paths."""
    if name not in SMALL:
        raise SystemExit(f"--fetch takes one of {', '.join(SMALL)}: {name!r}")
    out = data_dir / name
    with httpx.Client(timeout=600, follow_redirects=True) as client:
        r = client.get(RELEASE.format(name=name))
        r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for member in KEPT:
            target = out / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(f"{name}/{member}"))
    print(f"wrote {', '.join(KEPT)} under {out}", file=sys.stderr)
    return out


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load(root: Path) -> tuple[list[dict], dict[str, str], dict[str, dict[str, int]]]:
    """(the corpus as knowledge documents, the test queries by id, their qrels by query
    id then document id). A query no judgement names is left out; a judgement of 0
    stays in the qrels and counts as not relevant."""
    qrels: dict[str, dict[str, int]] = {}
    for line in (root / "qrels" / "test.tsv").read_text().splitlines()[1:]:
        if line.strip():
            qid, doc_id, score = line.split("\t")
            qrels.setdefault(qid, {})[doc_id] = int(score)
    queries = {q["_id"]: q["text"] for q in _jsonl(root / "queries.jsonl") if q["_id"] in qrels}
    docs = []
    for row in _jsonl(root / "corpus.jsonl"):
        title, text = row.get("title") or "", row.get("text") or ""
        if text or title:  # a passage with neither has nothing to index
            uri = f"{URI_PREFIX}{row['_id']}"
            docs.append({"source_uri": uri, "title": title, "content": text or title})
    return docs, queries, {q: qrels[q] for q in queries}


def corpus_tag(docs: list[dict]) -> str:
    """A tag naming this exact corpus, so a search refuses a namespace holding another."""
    h = hashlib.sha256()
    for d in docs:
        h.update(json.dumps(d, sort_keys=True).encode())
    return f"beir:{h.hexdigest()[:12]}"


def run_namespace(named: str | None) -> tuple[str, bool]:
    """(the namespace a run uses, whether it outlives the run)."""
    if named:
        if not named.startswith(PREFIX) or named.startswith(FRESH_PREFIX):
            raise SystemExit(
                f"--namespace must start with {PREFIX!r}, not {FRESH_PREFIX!r}: {named!r}"
            )
        return named, True
    return f"{FRESH_PREFIX}{secrets.token_hex(4)}", False


def ingest(post: SyncPost, org: str, namespace: str, docs: list[dict], keep: bool) -> None:
    if keep:  # printed first, so a failed ingest leaves the command behind
        print(f"kept; erase it with: {gc_command(org, [namespace])}", file=sys.stderr)
    register_bench_namespace_sync(
        post, org=org, namespace=namespace, description="BEIR corpus", keep=keep
    )
    remember_bench_namespace(org, namespace)
    tag = corpus_tag(docs)
    for i in range(0, len(docs), BATCH):
        entries = [{**d, "tags": [tag]} for d in docs[i : i + BATCH]]
        post("/v1/knowledge.bulk_add", {"org": org, "namespace": namespace, "entries": entries})
        print(f"ingested {min(i + BATCH, len(docs))}/{len(docs)}", file=sys.stderr)


def check_corpus(post: SyncPost, org: str, namespace: str, docs: list[dict]) -> int:
    """Refuse a namespace that does not hold exactly this corpus; return its chunk count."""
    tag = corpus_tag(docs)
    rows = listed_documents(post, org, namespace)
    have = {row["source_uri"] for row in rows}
    want = {d["source_uri"] for d in docs}
    stale = sum(tag not in (row.get("tags") or []) for row in rows)
    if have != want or stale:
        raise SystemExit(
            f"{org}/{namespace} is not this corpus: missing {len(want - have)}, "
            f"extra {len(have - want)}, from another version {stale}"
        )
    return sum(int(row.get("chunk_count") or 0) for row in rows)


def ranked_docs(hits: list[dict]) -> list[str]:
    """The documents of `hits` in rank order, each once, at its first chunk's rank."""
    out: list[str] = []
    for h in hits:
        doc_id = h["source_uri"].removeprefix(URI_PREFIX)
        if doc_id not in out:
            out.append(doc_id)
    return out


def score(ranked: list[str], qrels: dict[str, int]) -> dict[str, float]:
    """nDCG@10 with the graded judgement as the gain (the BEIR convention), recall@10
    and @100 over the documents judged relevant (> 0), and MRR@10."""
    relevant = {d for d, g in qrels.items() if g > 0}
    dcg = sum(qrels.get(d, 0) / math.log2(r + 2) for r, d in enumerate(ranked[:10]))
    ideal_gains = sorted((g for g in qrels.values() if g > 0), reverse=True)[:10]
    ideal = sum(g / math.log2(r + 2) for r, g in enumerate(ideal_gains))
    first = next((r + 1 for r, d in enumerate(ranked[:10]) if d in relevant), None)
    n = len(relevant) or 1
    return {
        "ndcg@10": dcg / ideal if ideal else 0.0,
        "recall@10": len(relevant & set(ranked[:10])) / n,
        "recall@100": len(relevant & set(ranked[:100])) / n,
        "mrr@10": 1.0 / first if first else 0.0,
    }


def search(
    post: SyncPost,
    org: str,
    namespace: str,
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
    params: dict,
) -> dict:
    rows = []
    reranked = 0
    for qid, text in queries.items():
        body = {
            **params,
            "org": org,
            "namespace": namespace,
            "namespace_scope": "this",
            "query": text,
            "top_k": DEPTH,
            "collapse_sections": False,
        }
        out = post("/v1/knowledge.search", body)
        if out.get("ignored_args"):
            raise SystemExit(f"knowledge.search ignored {out['ignored_args']}: the arm did not run")
        reranked += bool(out.get("reranked_by"))
        metrics = score(ranked_docs(out["results"]), qrels[qid])
        rows.append({"id": qid, "cluster": qid, "metrics": metrics})
    keys = rows[0]["metrics"] if rows else {}
    means = {k: round(sum(r["metrics"][k] for r in rows) / len(rows), 4) for k in keys}
    return {"params": params, "metrics": means, "reranked": reranked, "queries": rows}


# Fixed here besides knowledge_retrieval.py's depth, scope and filters: documents are
# scored once each, so a collapse would only drop hits.
FIXED_FIELDS = ("collapse_sections",)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fetch", metavar="DATASET", help=f"download one of: {', '.join(SMALL)}")
    ap.add_argument("--data-dir", type=Path, help="where --fetch writes the dataset")
    ap.add_argument("--data", type=Path, help="a dataset directory (corpus, queries, qrels)")
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="bench")
    ap.add_argument(
        "--namespace",
        help=f"the namespace to ingest into or search, never erased; starts with {PREFIX!r} "
        "(default: a fresh one per run, erased at its end)",
    )
    ap.add_argument("--phase", choices=("ingest", "search", "both"), default="both")
    ap.add_argument("--param", action="append", default=[], help="knowledge.search KEY=VALUE")
    ap.add_argument("--out", type=Path, help="write the full report (per query) as JSON")
    args = ap.parse_args()
    if args.fetch:
        if not args.data_dir:
            ap.error("--fetch needs a --data-dir")
        fetch(args.fetch, args.data_dir)
        return 0
    if not args.data:
        ap.error("--data (a dataset directory) or --fetch is required")
    if args.phase != "both" and not args.namespace:
        ap.error(f"--phase {args.phase} needs a --namespace (one ingest, many searches)")

    for key in FIXED_FIELDS:
        if any(p.partition("=")[0] == key for p in args.param):
            raise SystemExit(f"--param {key} is refused: the harness fixes it")
    params = parse_params(args.param)
    namespace, keep = run_namespace(args.namespace)
    docs, queries, qrels = load(args.data)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    base = args.base_url.rstrip("/")
    with httpx.Client(headers=headers) as client:

        def post(path: str, body: dict[str, Any]) -> Any:
            return post_json(client, f"{base}{path}", body, attempts=3, timeout_s=300.0)

        health = client.get(f"{base}/v1/health", timeout=30)
        if health.status_code != 200:
            raise SystemExit(f"{base}/v1/health answered {health.status_code}: no run")
        instrument = run_metadata(health.json())
        require_real_embedder(instrument)
        instrument["dataset"] = args.data.name
        instrument["corpus"] = corpus_tag(docs)
        with erase_on_exit_sync(post, org=args.org, keep=keep):
            if args.phase in ("ingest", "both"):
                ingest(post, args.org, namespace, docs, keep)
            if args.phase == "ingest":
                return 0
            instrument["chunks"] = check_corpus(post, args.org, namespace, docs)
            report = search(post, args.org, namespace, queries, qrels, params)
    report = {"instrument": instrument, **report}
    if args.out:
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "queries"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
