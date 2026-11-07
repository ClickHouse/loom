"""BEIR passage retrieval through `knowledge.search`: nDCG@10, recall@10/100, MRR@10.

A BEIR dataset is a corpus of passages, a set of queries and graded relevance
judgements (qrels). Each passage is ingested as one document (`beir:<doc id>`,
its title and text), each test query with a judgement is searched, and the hits
are scored against the qrels by document, the passage's chunks counted once at
the rank of the first and a hit on the query's own id dropped (ArguAna's queries
are corpus passages; BEIR's evaluation drops them the same way). `--fetch`
downloads one of the small datasets (`SMALL`) from the BEIR release, checks the
archive against its published MD5 and keeps only its corpus, queries and test
qrels:

    PYTHONPATH=$PWD .venv/bin/python bench/beir_knowledge.py --fetch scifact  # bench/datasets/beir

Then ingest once into a namespace you name, which is kept, and search it per arm:

    PYTHONPATH=$PWD .venv/bin/python bench/beir_knowledge.py --data bench/datasets/beir/scifact \\
        --base-url http://localhost:7790 --namespace beir-scifact --phase ingest
    PYTHONPATH=$PWD .venv/bin/python bench/beir_knowledge.py --data bench/datasets/beir/scifact \\
        --base-url http://localhost:7790 --namespace beir-scifact --phase search \\
        --param search_mode=hybrid --out /tmp/beir-hybrid.json

Without `--namespace` both phases run into a fresh namespace erased at the end.
A named namespace must start with `beir-` (and not `beir-run-`, a run's own), as
`knowledge_retrieval.py` does with its prefix. An ingest adds only the passages
the namespace does not hold yet, so a failed one is resumed by running it again.
A search over a namespace that does not hold exactly this corpus, embedded with
the server's current model, fails the run, and so does one that finds nothing.
`--param KEY=VALUE` sets one ranking knob of `knowledge.search`, as
`knowledge_retrieval.py` does, except `collapse_sections`, which the harness
fixes; filters, scope and depth are refused. An arm that names no reranker runs
with `rerank="none"`, whatever the deployment default. A run records the
server's embedding model and build and the corpus, queries and judgements it
scored, and refuses a fake embedder; a hybrid or lexical arm whose lexical leg
never fired, and an arm whose reranker did not run as it says
(`check_reranked`), fail.

A search returns at most 100 hits, which are chunks: recall@100 is over the
documents they hold (`depth` in the report: 100 when every passage is one chunk,
at most 99 on ArguAna, whose query finds its own passage and drops it). Ingest
cost is the corpus's embedding tokens (SciFact ≈ 5.2K passages, NFCorpus ≈ 3.6K,
ArguAna ≈ 8.7K, SCIDOCS ≈ 25K, FiQA ≈ 57K, each embedded by its own call); a
search arm embeds only its queries, plus one model call per query when it
reranks. Two arms pair by query id (`bench.bootstrap.paired_bootstrap`, each
query its own cluster).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
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
from bench.knowledge_retrieval import (
    check_corpus,
    check_reranked,
    digest,
    listed_documents,
    parse_params,
    require_real_embedder,
    run_namespace,
)
from bench.quality import run_metadata
from server.knowledge.api.models_knowledge import KnowledgeSearchInput
from server.knowledge.domain.documents import MAX_SEARCH_TOP_K

RELEASE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{name}.zip"
# The small datasets and the MD5 the BEIR release publishes for each archive.
SMALL = {
    "scifact": "5f7d1de60b170fc8027bb7898e2efca1",
    "nfcorpus": "a89dba18a62ef92f7d323ec890a0d38d",
    "arguana": "8ad3e3c2a5867cdced806d6503f29b99",
    "scidocs": "38121350fc3a4d2f48850f6aff52e4a9",
    "fiqa": "17918ed23cd04fb15047f73e6c3bd9d9",
}
# What a fetch keeps from the release archive: nothing else is written.
KEPT = ("corpus.jsonl", "queries.jsonl", "qrels/test.tsv")
DATA_DIR = Path(__file__).parent / "datasets" / "beir"  # git-ignored
PREFIX = "beir-"
URI_PREFIX = "beir:"
DEPTH = MAX_SEARCH_TOP_K  # hits a search asks for: the wire bound
# Documents per knowledge.bulk_add (the op takes 500): each is embedded by its own call,
# so a batch must finish inside the client timeout on a slow embedder.
BATCH = 50


def fetch(name: str, data_dir: Path) -> Path:
    """Download one BEIR dataset, check the archive's MD5, and write its corpus,
    queries and test qrels under `data_dir/name`. Archive members are read by name,
    never extracted as paths."""
    if name not in SMALL:
        raise SystemExit(f"--fetch takes one of {', '.join(SMALL)}: {name!r}")
    with httpx.Client(timeout=600, follow_redirects=True) as client:
        r = client.get(RELEASE.format(name=name))
        r.raise_for_status()
    md5 = hashlib.md5(r.content, usedforsecurity=False).hexdigest()
    if md5 != SMALL[name]:
        raise SystemExit(f"{name}.zip has MD5 {md5}, not the published {SMALL[name]}")
    out = data_dir / name
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for member in KEPT:
            target = out / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(f"{name}/{member}"))
    print(f"wrote {', '.join(KEPT)} under {out}", file=sys.stderr)
    return out


def _jsonl(path: Path) -> list[dict]:
    # Read line by line: `str.splitlines` also breaks on U+2028 and its kin, which
    # JSON allows unescaped inside a string.
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load(root: Path) -> tuple[list[dict], dict[str, str], dict[str, dict[str, int]]]:
    """(the corpus as knowledge documents, the test queries by id, their qrels by query
    id then document id). A query no judgement names is left out; a judgement of 0
    stays in the qrels and counts as not relevant."""
    qrels: dict[str, dict[str, int]] = {}
    for line in (root / "qrels" / "test.tsv").read_text(encoding="utf-8").splitlines()[1:]:
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
    if not queries:
        raise SystemExit(f"{root}: no query in queries.jsonl has a test judgement")
    return docs, queries, {q: qrels[q] for q in queries}


def corpus_tag(docs: list[dict], embedding_model: str) -> str:
    """A tag naming this exact corpus and the model its chunks were embedded with: a
    search only reads chunks of the server's current model, so a namespace ingested
    under another would score as empty (or lexical only) without failing."""
    return f"beir:{digest([docs, embedding_model])}"


def ingest(
    post: SyncPost, org: str, namespace: str, docs: list[dict], tag: str, keep: bool
) -> None:
    """Add the passages the namespace does not hold yet. `knowledge.bulk_add` never
    supersedes a document's chunks, so a passage sent twice would have its chunks
    indexed twice: one already there with its chunks is skipped (one an interrupted
    write left without chunks is sent again), and the caller sends each batch once. A
    namespace holding anything but this corpus under this model (`tag`) is refused
    before anything is written. A batch of 50 embeds in well under the client timeout;
    after one times out anyway, run the ingest again only once the server has finished
    writing it."""
    rows = listed_documents(post, org, namespace)
    want = {d["source_uri"] for d in docs}
    foreign = sum(r["source_uri"] not in want or tag not in (r.get("tags") or []) for r in rows)
    if foreign:
        raise SystemExit(
            f"{org}/{namespace} holds {foreign} documents of another corpus or embedding model"
        )
    if keep:  # printed first, so a failed ingest leaves the command behind
        print(f"kept; erase it with: {gc_command(org, [namespace])}", file=sys.stderr)
    register_bench_namespace_sync(
        post, org=org, namespace=namespace, description="BEIR corpus", keep=keep
    )
    remember_bench_namespace(org, namespace)
    held = {r["source_uri"] for r in rows if r.get("chunk_count")}
    todo = [d for d in docs if d["source_uri"] not in held]
    for i in range(0, len(todo), BATCH):
        entries = [{**d, "tags": [tag]} for d in todo[i : i + BATCH]]
        post("/v1/knowledge.bulk_add", {"org": org, "namespace": namespace, "entries": entries})
        print(f"ingested {min(i + BATCH, len(todo))}/{len(todo)}", file=sys.stderr)


def ranked_docs(hits: list[dict], query_id: str) -> list[str]:
    """The documents of `hits` in rank order, each once, at its first chunk's rank, and
    never the query's own id."""
    out: list[str] = []
    for h in hits:
        doc_id = h["source_uri"].removeprefix(URI_PREFIX)
        if doc_id != query_id and doc_id not in out:
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
    reranked = lexical_fired = 0
    # An arm that names no reranker runs without one, whatever the deployment default.
    rerank = params.get("rerank") or "none"
    for qid, text in queries.items():
        body = {
            **params,
            "rerank": rerank,
            "org": org,
            "namespace": namespace,
            "namespace_scope": "this",
            "query": text,
            "top_k": DEPTH,
            "collapse_sections": False,  # each document is scored once already
        }
        out = post("/v1/knowledge.search", body)
        if out.get("ignored_args"):
            raise SystemExit(f"knowledge.search ignored {out['ignored_args']}: the arm did not run")
        hits = out["results"]
        reranked += bool(out.get("reranked_by"))
        lexical_fired += any(h.get("lexical_score", 0) > 0 for h in hits)
        ranked = ranked_docs(hits, qid)
        metrics = {**score(ranked, qrels[qid]), "depth": float(len(ranked))}
        rows.append({"id": qid, "cluster": qid, "metrics": metrics})
    if not any(r["metrics"]["depth"] for r in rows):
        raise SystemExit(f"no query found anything in {org}/{namespace}: was it ingested?")
    mode = params.get("search_mode", KnowledgeSearchInput.model_fields["search_mode"].default)
    if mode != "semantic" and not lexical_fired:
        raise SystemExit("the lexical leg never fired: the arm ran as semantic")
    check_reranked(rerank, reranked)
    keys = rows[0]["metrics"]
    means = {k: round(sum(r["metrics"][k] for r in rows) / len(rows), 4) for k in keys}
    return {
        "params": {**params, "rerank": rerank},  # as sent
        "metrics": means,
        "reranked": reranked,
        "lexical_fired": lexical_fired,
        "queries": rows,
    }


def arm_params(pairs: list[str]) -> dict:
    """`parse_params`, refusing `collapse_sections` too: the harness scores documents."""
    params = parse_params(pairs)
    if "collapse_sections" in params:
        raise SystemExit("--param collapse_sections is refused: the harness fixes it")
    return params


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fetch", metavar="DATASET", help=f"download one of: {', '.join(SMALL)}")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR, help="where --fetch writes")
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
        fetch(args.fetch, args.data_dir)
        return 0
    if not args.data:
        ap.error("--data (a dataset directory) or --fetch is required")
    if args.phase != "both" and not args.namespace:
        ap.error(f"--phase {args.phase} needs a --namespace (one ingest, many searches)")

    params = arm_params(args.param)
    namespace, keep = run_namespace(args.namespace, PREFIX)
    docs, queries, qrels = load(args.data)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    base = args.base_url.rstrip("/")
    with httpx.Client(headers=headers) as client:

        def post(path: str, body: dict[str, Any]) -> Any:
            # A resent bulk_add after a 5xx could write its batch twice.
            attempts = 1 if path.endswith("bulk_add") else 3
            return post_json(client, f"{base}{path}", body, attempts=attempts, timeout_s=300.0)

        health = client.get(f"{base}/v1/health", timeout=30)
        if health.status_code != 200:
            raise SystemExit(f"{base}/v1/health answered {health.status_code}: no run")
        instrument = run_metadata(health.json())
        require_real_embedder(instrument)
        instrument["dataset"] = args.data.name
        tag = corpus_tag(docs, instrument["embedding_model"])
        instrument["corpus"] = tag
        instrument["queries"] = digest(queries)
        instrument["qrels"] = digest(qrels)
        with erase_on_exit_sync(post, org=args.org, keep=keep):
            if args.phase in ("ingest", "both"):
                ingest(post, args.org, namespace, docs, tag, keep)
            if args.phase == "ingest":
                return 0
            instrument["chunks"] = check_corpus(post, args.org, namespace, docs, tag)
            report = search(post, args.org, namespace, queries, qrels, params)
    report = {"instrument": instrument, **report}
    if args.out:
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "queries"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
