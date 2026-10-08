"""Retrieval-vs-evidence alignment for loom.

Descriptive similarity can be a WEAK predictor of whether a memory is the gold
EVIDENCE for the answer (a within-query AUC near 0.5 on LoCoMo would mean rank
ignores evidence). This bench measures loom's own alignment so the question is
answered for loom rather than assumed, and so a regression in it is caught.

For each LoCoMo question that cites gold evidence, it runs `memory.search` and,
using the hit `session_id` (`<ns>:session_N`) against the question's cited
evidence sessions, reports:

  within-query AUC   P(a gold-evidence hit outranks a non-gold hit); 0.5 = rank
                     is independent of evidence, ~1.0 = ranking IS evidence-aware
  gold-recall@k      share of the gold-evidence SESSIONS present in the top-k
                     window (loom's source_recall_at), k in {5,10,20}

Granularity note: this is SESSION-level (loom's session_id), coarser than
per-turn evidence, so absolute recall reads higher than turn-level numbers;
the AUC is the fair cross-system signal. Reuses the LoCoMo
helpers in `locomo_qa.py`. Ingests its own namespaces (prefix `bench-ev-align`) so it
does not depend on another run's lifecycle; `--reuse` skips ingest.

Usage:
  LOOM_API_KEY=... python bench/evidence_alignment.py \\
      --dataset bench/datasets/locomo10.json --base-url http://localhost:7788 \\
      --limit-conversations 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from locomo_qa import (
    evidence_sessions_cited,
    hit_session_numbers,
    iter_sessions,
    source_recall_at,
    turn_to_message,
)


async def _post(cl: httpx.AsyncClient, url: str, body: dict, tok: str) -> dict:
    for _ in range(4):
        try:
            r = await cl.post(
                url, json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=180
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503):
                await asyncio.sleep(3)
                continue
            return {"_err": r.status_code}
        except (httpx.HTTPError, ValueError):
            await asyncio.sleep(3)
    return {"_err": "retries"}


def within_query_auc(is_gold: list[bool]) -> float | None:
    """AUC over rank-ordered hits: P(gold hit ranks above non-gold hit)."""
    golds = [i for i, g in enumerate(is_gold) if g]
    negs = [i for i, g in enumerate(is_gold) if not g]
    if not golds or not negs:
        return None
    wins = sum(1 for gi in golds for ni in negs if gi < ni)
    return wins / (len(golds) * len(negs))


async def ingest_conv(
    cl, base, tok, sem, ns, conv, *, org: str = "dev", agent: str = "ev-align"
) -> None:
    convo = conv.get("conversation") or conv

    async def one(key, iso, turns):
        body = {
            "org": org,
            "namespace": ns,
            "agent": agent,
            "session_id": f"{ns}:{key}",
            "messages": [turn_to_message(t) for t in turns],
            "max_tokens": 4096,
        }
        if iso:
            body["observation_date"] = iso
        async with sem:
            await _post(cl, base + "/v1/memory.set_from_messages", body, tok)

    await asyncio.gather(*(one(k, iso, t) for k, iso, t in iter_sessions(convo)))


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="bench/datasets/locomo10.json")
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7788"))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--org", default="dev")
    p.add_argument("--namespace-prefix", default="bench-ev-align")
    p.add_argument("--limit-conversations", type=int, default=3)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--reuse", action="store_true", help="skip ingest; reuse existing namespaces")
    args = p.parse_args()
    tok = args.token
    if not tok:
        print("need --token or LOOM_API_KEY", file=sys.stderr)
        return 2
    data = json.load(open(args.dataset))[: args.limit_conversations]
    sem = asyncio.Semaphore(args.concurrency)
    ns_of = {i: f"{args.namespace_prefix}-{i}" for i in range(len(data))}

    aucs: list[float] = []
    gold_ranks: list[int] = []
    recall: dict[int, list[float]] = {5: [], 10: [], 20: []}
    n_q = 0

    async with httpx.AsyncClient() as cl:
        if not args.reuse:
            for i, conv in enumerate(data):
                await ingest_conv(cl, args.base_url, tok, sem, ns_of[i], conv, org=args.org)
                print(f"  ingested conv {i} -> {ns_of[i]}", file=sys.stderr, flush=True)

        async def diag(q: dict, ns: str) -> None:
            nonlocal n_q
            if int(q.get("category", 0)) == 5:  # adversarial cites no evidence
                return
            gold = evidence_sessions_cited(q)
            if not gold:
                return
            async with sem:
                resp = await _post(
                    cl,
                    args.base_url + "/v1/memory.search",
                    {
                        "org": args.org,
                        "namespace": ns,
                        "query": str(q.get("question") or ""),
                        "top_k": args.top_k,
                        "namespace_scope": "this",
                    },
                    tok,
                )
            hits = list(resp.get("results") or [])
            if not hits:
                return
            sess = hit_session_numbers(hits, ns)
            is_gold = [s in gold for s in sess]
            n_q += 1
            auc = within_query_auc(is_gold)
            if auc is not None:
                aucs.append(auc)
            gold_ranks.extend(i for i, g in enumerate(is_gold) if g)
            for k in (5, 10, 20):
                r = source_recall_at(sess, gold, k)
                if r is not None:
                    recall[k].append(r)

        tasks = [diag(q, ns_of[i]) for i, conv in enumerate(data) for q in conv.get("qa", [])]
        await asyncio.gather(*tasks)

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else float("nan")

    print(
        f"=== loom retrieval-vs-evidence alignment "
        f"(LoCoMo {len(data)} conv, {n_q} evidence-bearing Qs) ==="
    )
    print(f"within-query AUC (rank predicts gold evidence): {mean(aucs):.3f}   (0.5=random)")
    print(f"mean 0-indexed rank of gold hits: {mean(gold_ranks):.2f} of top {args.top_k}")
    for k in (5, 10, 20):
        print(f"gold-evidence recall@{k}: {100 * mean(recall[k]):.1f}%   (n={len(recall[k])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
