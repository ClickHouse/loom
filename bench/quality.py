"""Loom quality bench — measures memory_key recall@K against a
question-answering dataset.

What this measures: for each conversation, run memory.set_from_messages
to populate Loom with extracted facts. Then for each held-out question,
run memory.search and check whether any of the question's gold answer
keys appears as a substring of a returned memory_key (or content
excerpt). Recall@K = fraction of questions for which at least one gold
key was recovered in the top-K results.

What this *doesn't* measure: answer accuracy. Published end-to-end
scores on LoCoMo and LongMemEval grade the system's *generated
answer* against a gold answer via an LLM judge — that's a heavier
harness with a per-question LLM call. memory_key recall is the IR
baseline — it answers "did the right memory get extracted and is
it retrievable" without conflating it with answer-generation quality.

Dataset format: see bench/datasets/synthetic_memory_qa.json.

Run:
    python bench/quality.py --dataset bench/datasets/synthetic_memory_qa.json \\
        --base-url http://localhost:7777 --top-k 10 --out bench/quality.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

try:
    from bench._http import post_json
    from bench.bench_namespaces import (
        erase_on_exit_sync,
        register_bench_namespace_sync,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/quality.py` puts bench/ itself on sys.path
    from _http import post_json  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit_sync,
        register_bench_namespace_sync,
        remember_bench_namespace,
    )


@dataclass
class QuestionResult:
    conversation_id: str
    question: str
    gold_keys: list[str]
    top_k_keys: list[str]
    top_k_excerpts: list[str]
    recalled: bool
    # 1-based rank of the first gold hit; 0 when none landed in the top k.
    first_gold_rank: int = 0
    # gold-matching hits among the returned ones (precision numerator).
    gold_hits: int = 0


@dataclass(frozen=True)
class Summary:
    """The run's headline numbers. Recall alone rewards dumping the store:
    a run that returns every memory scores recall 1.0 and is useless to an
    agent that reads from the top. MRR says where the gold sat; precision@k
    says how much of what came back was noise (score the noise, not only the
    hit)."""

    total: int
    recalled: int
    recall: float
    mrr: float
    precision_at_k: float
    rank1: float


def summarise(results: list[QuestionResult]) -> Summary:
    n = len(results)
    if not n:
        return Summary(0, 0, 0.0, 0.0, 0.0, 0.0)
    recalled = sum(1 for r in results if r.recalled)
    mrr = sum((1.0 / r.first_gold_rank) for r in results if r.first_gold_rank) / n
    precision = sum((r.gold_hits / len(r.top_k_keys)) for r in results if r.top_k_keys) / n
    rank1 = sum(1 for r in results if r.first_gold_rank == 1) / n
    return Summary(n, recalled, recalled / n, mrr, precision, rank1)


def run_metadata(health: dict[str, object]) -> dict[str, object]:
    """The instrument a run was produced under. Two recall numbers are only
    comparable under the same embedder and server build (an embedding-model
    swap can move accuracy by several points and reverse a conclusion), so
    every run records them next to its numbers."""
    return {
        "embedding_model": str(health.get("embedding_model") or ""),
        "embedding_dim": (
            health.get("embedding_dim_probed") or health.get("embedding_dim_configured")
        ),
        "llm_model": str(health.get("llm_model") or ""),
        "llm_provider": str(health.get("llm_provider") or ""),
        "build_commit": str(health.get("build_commit") or ""),
    }


def _post(
    client: httpx.Client,
    base_url: str,
    path: str,
    body: dict[str, object],
    token: str = "",
) -> dict[str, object]:
    out: dict[str, object] = post_json(client, base_url + path, body, token=token, timeout_s=30.0)
    return out


def _gold_hit(gold_keys: list[str], memory_key: str, excerpt: str) -> bool:
    """A retrieval is correct if any gold key appears as a substring of
    the memory_key (case-insensitive, with underscores normalised) OR
    of the content excerpt. We're lenient because LLM-extracted keys
    vary across runs ("user_dog_name" vs "users_pet" both valid)."""
    key_norm = memory_key.lower().replace("_", " ")
    excerpt_norm = excerpt.lower()
    for gold in gold_keys:
        g = gold.lower().replace("_", " ")
        if g in key_norm or g in excerpt_norm:
            return True
    return False


def _run_one_conversation(
    client: httpx.Client,
    base_url: str,
    namespace: str,
    token: str,
    item: dict[str, object],
    top_k: int,
    org: str = "dev",
) -> list[QuestionResult]:
    identity = {
        "org": org,
        "namespace": namespace,
        "agent": "tracker-bench",
        "user_id": "-",
    }

    # Phase 1 — extract memories from the conversation.
    _post(
        client,
        base_url,
        "/v1/memory.set_from_messages",
        {**identity, "messages": item["conversation"], "max_tokens": 1024},
        token,
    )

    # Phase 2 — for each question, search and grade.
    results: list[QuestionResult] = []
    for q in item["questions"]:
        resp = _post(
            client,
            base_url,
            "/v1/memory.search",
            {
                **identity,
                "query": q["question"],
                "top_k": top_k,
                "search_mode": "hybrid",
                "alpha": 0.5,
            },
            token,
        )
        hits = resp.get("results", [])
        top_keys = [h["memory_key"] for h in hits]
        top_excerpts = [h.get("content_excerpt", "") for h in hits]
        gold_flags = [
            _gold_hit(q["gold_keys"], k, e) for k, e in zip(top_keys, top_excerpts, strict=True)
        ]
        recalled = any(gold_flags)
        first_rank = next((i + 1 for i, g in enumerate(gold_flags) if g), 0)
        results.append(
            QuestionResult(
                conversation_id=str(item["conversation_id"]),
                question=str(q["question"]),
                gold_keys=list(q["gold_keys"]),
                top_k_keys=top_keys,
                top_k_excerpts=top_excerpts,
                recalled=recalled,
                first_gold_rank=first_rank,
                gold_hits=sum(1 for g in gold_flags if g),
            )
        )
    return results


def gate(recalled: int, total: int, min_recall: float) -> tuple[int, str]:
    """`(exit_code, message)` for the recall gate. Pure, so the gate is unit-tested
    without the live control plane, embedder and extraction LLM a run needs.

    `total == 0` fails rather than passing vacuously: a dataset that produced no
    questions has measured nothing, and 0/0 must not read as "met the bar".
    """
    if total <= 0:
        return 1, "FAIL dataset produced no questions — no measurement was taken"
    frac = recalled / total
    if frac < min_recall:
        return 1, f"FAIL recall {frac:.3f} < --min-recall {min_recall}"
    return 0, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="bench/datasets/synthetic_memory_qa.json")
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--out", default="bench/quality.csv")
    parser.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    # Same gate flag as bench/retrieval_recall.py.
    parser.add_argument(
        "--min-recall",
        type=float,
        default=0.0,
        help="Fail (exit 1) when recall@top_k falls below this. Default 0 = report only.",
    )
    parser.add_argument("--org", default="dev")
    parser.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="do not erase this run's qabench-* namespaces when it ends",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    with dataset_path.open() as f:
        dataset = json.load(f)

    print(
        f"loom-quality against {args.base_url}, dataset={dataset_path}, "
        f"top_k={args.top_k}, conversations={len(dataset)}"
    )

    results: list[QuestionResult] = []
    with httpx.Client() as client:
        # Health gate
        h = client.get(args.base_url + "/v1/health", timeout=5.0)
        if h.status_code != 200:
            print(f"server unhealthy: {h.status_code}", file=sys.stderr)
            return 2
        try:
            meta = run_metadata(h.json())
        except ValueError:
            meta = run_metadata({})
        print("instrument: " + ", ".join(f"{k}={v}" for k, v in meta.items() if v), flush=True)

        def post(path: str, body: dict[str, object]) -> dict[str, object]:
            return _post(client, args.base_url, path, body, args.token)

        with erase_on_exit_sync(post, org=args.org, keep=args.keep_namespaces):
            for item in dataset:
                namespace = f"qabench-{uuid.uuid4().hex[:8]}"
                print(f"  [{item['conversation_id']}] {namespace}", flush=True)
                remember_bench_namespace(args.org, namespace)
                register_bench_namespace_sync(
                    post,
                    org=args.org,
                    namespace=namespace,
                    description=f"quality bench {item['conversation_id']}",
                    keep=args.keep_namespaces,
                )
                results.extend(
                    _run_one_conversation(
                        client, args.base_url, namespace, args.token, item, args.top_k, args.org
                    )
                )

    # No error fraction to tolerate: `_post` raises on any HTTP failure, which
    # aborts the run with a non-zero exit (after the namespaces are erased).
    summary = summarise(results)
    n, recalled = summary.total, summary.recalled
    rate = summary.recall * 100.0

    # Per-conversation breakdown
    by_conv: dict[str, list[bool]] = {}
    for r in results:
        by_conv.setdefault(r.conversation_id, []).append(r.recalled)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "conversation_id",
                "question",
                "gold_keys",
                "recalled",
                "first_gold_rank",
                "gold_hits",
                "returned",
                "top_k_keys",
            ]
        )
        for r in results:
            w.writerow(
                [
                    r.conversation_id,
                    r.question,
                    "|".join(r.gold_keys),
                    "1" if r.recalled else "0",
                    str(r.first_gold_rank),
                    str(r.gold_hits),
                    str(len(r.top_k_keys)),
                    "|".join(r.top_k_keys),
                ]
            )
    # The instrument rides a sidecar so a CSV diff across runs can be read
    # for what changed: the ranking, or the embedder under it.
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(
        json.dumps(
            {
                **meta,
                "top_k": args.top_k,
                "questions": n,
                "recall": round(summary.recall, 4),
                "mrr": round(summary.mrr, 4),
                "precision_at_k": round(summary.precision_at_k, 4),
                "rank1": round(summary.rank1, 4),
            },
            indent=1,
        )
    )

    print()
    print(f"{'conversation_id':<22} {'recalled / total':>20}")
    for cid, hits in sorted(by_conv.items()):
        print(f"{cid:<22} {sum(hits):>8} / {len(hits):>3}  ({sum(hits) / len(hits) * 100.0:.1f}%)")
    print()
    print(f"OVERALL recall@{args.top_k}: {recalled} / {n}  ({rate:.1f}%)")
    print(
        f"MRR {summary.mrr:.3f} · precision@{args.top_k} {summary.precision_at_k:.3f} "
        f"· gold ranked first {summary.rank1:.3f}"
    )
    print(f"\nwrote {out_path} (+ {meta_path.name})")

    code, msg = gate(recalled, n, args.min_recall)
    if msg:
        print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
