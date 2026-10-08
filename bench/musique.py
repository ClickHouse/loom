"""MuSiQue multi-hop QA against loom — the public yardstick.

A 200-question MuSiQue-Ans subset is a common multi-hop yardstick for memory
graphs, with published EM/F1 for BM25, dense top-3 and full-context
baselines. This bench puts loom's retrieval on that yardstick and reports the
retrieval metric separately from the reader so an embedding or
reader swap cannot masquerade as a memory-architecture gain.

Protocol per question (fresh namespace each):
  1. every paragraph of the question (20; 2–4 are `is_supporting`) is written
     with memory.set as memory_key "p<idx>", value = title + text;
  2. memory.search(question, search_mode=..., top_k) → hits;
  3. supporting-paragraph recall@k from the hit keys (reader-independent);
  4. optional: a fixed reader (BENCH_READER_MODEL) answers from the hits;
     EM/F1 against answer + answer_aliases with the official MuSiQue
     normalisation (lowercase, strip punctuation and articles).

Data: MuSiQue-Ans dev (CC BY 4.0), `--data path/to/musique_ans_v1.0_dev.jsonl`
(from the MuSiQue release). The 200-question sample is deterministic (`--seed 0`).

    python bench/musique.py --data musique_ans_v1.0_dev.jsonl --n 200 \
        --base-url http://localhost:7789 --search-json '{"search_mode":"graph"}' \
        [--reader]  # adds EM/F1 via OPENAI_API_KEY

Prints one JSON summary and, with --out, per-question rows.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import string
import sys
import time
import uuid

try:
    from bench._http import apost_json
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from _http import apost_json  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
from collections import Counter
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pure scoring — the official MuSiQue / SQuAD normalisation.
# ---------------------------------------------------------------------------

_ARTICLES = re.compile(r"\b(a|an|the)\b")


def normalize_answer(s: str) -> str:
    s = (s or "").lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = _ARTICLES.sub(" ", s)
    return " ".join(s.split())


def exact_match(prediction: str, golds: list[str]) -> float:
    p = normalize_answer(prediction)
    return 1.0 if any(p == normalize_answer(g) for g in golds if g) else 0.0


def f1_score(prediction: str, golds: list[str]) -> float:
    p_tokens = normalize_answer(prediction).split()
    best = 0.0
    for g in golds:
        if not g:
            continue
        g_tokens = normalize_answer(g).split()
        common = Counter(p_tokens) & Counter(g_tokens)
        overlap = sum(common.values())
        if overlap == 0:
            continue
        precision = overlap / len(p_tokens)
        recall = overlap / len(g_tokens)
        best = max(best, 2 * precision * recall / (precision + recall))
    return best


def supporting_recall(hit_keys: list[str], supporting_idx: set[int], k: int) -> float:
    """Fraction of supporting paragraphs whose memory_key ("p<idx>") appears
    in the first k hits. Reader-independent: the retrieval metric."""
    if not supporting_idx:
        return 0.0
    got = {int(key[1:]) for key in hit_keys[:k] if key.startswith("p") and key[1:].isdigit()}
    return len(got & supporting_idx) / len(supporting_idx)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def load_questions(path: Path, n: int, seed: int) -> list[dict[str, Any]]:
    """Deterministic sample of answerable questions from MuSiQue-Ans jsonl."""
    items: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("answerable") is False:
                continue
            items.append(d)
    rng = random.Random(seed)
    rng.shuffle(items)
    return items[: max(0, n)]


def paragraph_value(p: dict[str, Any]) -> str:
    title = str(p.get("title") or "").strip()
    text = str(p.get("paragraph_text") or "").strip()
    return f"{title}. {text}" if title else text


def golds_of(item: dict[str, Any]) -> list[str]:
    return [str(item.get("answer") or ""), *[str(a) for a in (item.get("answer_aliases") or [])]]


def hops_of(item: dict[str, Any]) -> int:
    ident = str(item.get("id") or "")
    m = re.match(r"(\d)hop", ident)
    return int(m.group(1)) if m else len(item.get("question_decomposition") or []) or 0


# ---------------------------------------------------------------------------
# Loom client (same shape as bench/longmemeval.py)
# ---------------------------------------------------------------------------


async def _post(client: Any, url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    out: dict[str, Any] = await apost_json(
        client, url, body, token=token, timeout_s=120.0, attempts=3
    )
    return out


_READER_PROMPT = (
    "Answer the question using only the passages. Reply with the shortest "
    "possible answer (a name, a date, a place), no explanation.\n\n"
    "Passages:\n{passages}\n\nQuestion: {question}\nAnswer:"
)


async def _read(
    client: Any, question: str, hits: list[dict[str, Any]], model: str, api_key: str
) -> str:
    passages = "\n".join(f"- {h.get('content_excerpt', '')}" for h in hits)
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    r = await client.post(
        base + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": _READER_PROMPT.format(passages=passages, question=question),
                }
            ],
        },
        timeout=60.0,
    )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


async def run_one(
    client: Any,
    item: dict[str, Any],
    *,
    base_url: str,
    token: str,
    org: str,
    agent: str,
    search_json: dict[str, Any],
    top_k: int,
    reader_model: str,
    api_key: str,
    write_mode: str = "set",
    namespace_salt: str = "",
    skip_write: bool = False,
    keep: bool = False,
) -> dict[str, Any]:
    # namespace_salt: the same salt and question give the same namespace, so
    # another arm can re-read a written store with skip_write (the write is
    # 20 paragraphs per question; the search is the thing under test).
    if namespace_salt:
        seed = f"{namespace_salt}|{item['id']}"
        ns = f"musique-{hashlib.sha1(seed.encode()).hexdigest()[:10]}"
    else:
        ns = f"musique-{uuid.uuid4().hex[:10]}"
        remember_bench_namespace(org, ns)  # throwaway: erased when the run exits
    identity = {"org": org, "namespace": ns, "agent": agent, "user_id": "-"}
    if not skip_write:
        # A salted store is written for other arms to re-read, so it is kept.
        await register_bench_namespace(
            lambda p, b: _post(client, base_url + p, b, token),
            org=org,
            namespace=ns,
            description=f"MuSiQue question {item['id']}",
            keep=keep or bool(namespace_salt),
        )
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(4)

    async def _write(p: dict[str, Any]) -> None:
        async with sem:
            await _post(
                client,
                base_url + "/v1/memory.set",
                {
                    **identity,
                    "memory_key": f"p{p['idx']}",
                    "value": paragraph_value(p),
                    "memory_type": "semantic",
                    "tags": ["musique"],
                },
                token,
            )

    if skip_write:
        pass
    elif write_mode == "extract":
        # One extraction call over all paragraphs: typed triples come from
        # this path, not from memory.set's LLM-free projection. The paragraph
        # index rides in the message text for the reader.
        await _post(
            client,
            base_url + "/v1/memory.set_from_messages",
            {
                **identity,
                "messages": [
                    {"role": "user", "content": f"[p{p['idx']}] {paragraph_value(p)}"}
                    for p in item["paragraphs"]
                ],
            },
            token,
        )
    else:
        await asyncio.gather(*[_write(p) for p in item["paragraphs"]])
    write_ms = int((time.perf_counter() - t0) * 1000)
    body = {**identity, "query": item["question"], "top_k": top_k, **search_json}
    t1 = time.perf_counter()
    resp = await _post(client, base_url + "/v1/memory.search", body, token)
    search_ms = int((time.perf_counter() - t1) * 1000)
    hits = resp.get("results", [])
    keys = [str(h.get("memory_key") or "") for h in hits]
    supporting = {int(p["idx"]) for p in item["paragraphs"] if p.get("is_supporting")}
    row: dict[str, Any] = {
        "id": item["id"],
        "hops": hops_of(item),
        "n_supporting": len(supporting),
        "write_ms": write_ms,
        "search_ms": search_ms,
        "hits": len(hits),
    }
    if write_mode != "extract":
        # Recall is scored from the p<idx> keys memory.set writes; extracted rows
        # carry their own keys, so an extract run reports no recall at all.
        for k in (2, 5, 10):
            row[f"recall@{k}"] = supporting_recall(keys, supporting, k)
    if reader_model and api_key:
        pred = await _read(client, item["question"], hits[:top_k], reader_model, api_key)
        golds = golds_of(item)
        row.update(
            {
                "prediction": pred,
                "em": exact_match(pred, golds),
                "f1": f1_score(pred, golds),
            }
        )
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}

    def mean(key: str) -> float | None:
        vals = [r[key] for r in rows if key in r]
        return round(sum(vals) / len(vals), 4) if vals else None

    out: dict[str, Any] = {"n": len(rows)}
    for key in ("recall@2", "recall@5", "recall@10", "em", "f1"):
        v = mean(key)
        if v is not None:
            out[key] = v
    by_hops: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_hops.setdefault(int(r.get("hops") or 0), []).append(r)

    def mean_of(rs: list[dict[str, Any]], key: str) -> dict[str, float]:
        vals = [r[key] for r in rs if key in r]
        return {key: round(sum(vals) / len(vals), 4)} if vals else {}

    out["by_hops"] = {
        str(h): {"n": len(rs), **mean_of(rs, "recall@5"), **mean_of(rs, "f1")}
        for h, rs in sorted(by_hops.items())
    }
    out["search_ms_p50"] = sorted(r["search_ms"] for r in rows)[len(rows) // 2]
    out["write_ms_p50"] = sorted(r["write_ms"] for r in rows)[len(rows) // 2]
    return out


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    items = load_questions(Path(args.data), args.n, args.seed)
    if not items:
        print("no questions loaded", file=sys.stderr)
        return 1
    search_json = json.loads(args.search_json) if args.search_json else {}
    api_key = os.environ.get("OPENAI_API_KEY", "") if args.reader else ""
    reader_model = (os.environ.get("BENCH_READER_MODEL") or "gpt-4o-mini") if args.reader else ""
    rows: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=120.0) as client:

        async def _one(item: dict[str, Any]) -> None:
            async with sem:
                try:
                    rows.append(
                        await run_one(
                            client,
                            item,
                            base_url=args.base_url.rstrip("/"),
                            token=args.token,
                            org=args.org,
                            agent="musique-bench",
                            search_json=search_json,
                            top_k=args.top_k,
                            reader_model=reader_model,
                            api_key=api_key,
                            write_mode=args.write_mode,
                            namespace_salt=args.namespace_salt,
                            skip_write=args.skip_write,
                            keep=args.keep_namespaces,
                        )
                    )
                except Exception as e:  # noqa: BLE001 — one bad item must not sink the run
                    rows.append(
                        {
                            "id": item.get("id"),
                            "error": str(e)[:200],
                            "hops": hops_of(item),
                            "search_ms": 0,
                            "write_ms": 0,
                            "hits": 0,
                            "n_supporting": 0,
                        }
                    )

        # Uuid-minted namespaces are erased on exit, after a failure or Ctrl-C
        # too; salted ones were never remembered and stay.
        async with erase_on_exit(
            lambda p, b: _post(client, args.base_url.rstrip("/") + p, b, args.token),
            org=args.org,
            keep=args.keep_namespaces,
        ):
            await asyncio.gather(*[_one(it) for it in items])
    summary = {
        "dataset": "musique_ans_dev",
        "seed": args.seed,
        "search_json": search_json,
        "reader": reader_model or None,
        "errors": sum(1 for r in rows if "error" in r),
        # Errored items are counted, not scored: recall and EM/F1 share one denominator.
        **summarize([r for r in rows if "error" not in r]),
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r) for r in rows))
    return 1 if summary["errors"] == len(rows) else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data", required=True, help="musique_ans_v1.0_dev.jsonl")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="dev")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument(
        "--search-json", default="", help='extra memory.search fields, e.g. {"search_mode":"graph"}'
    )
    ap.add_argument(
        "--reader", action="store_true", help="also answer with BENCH_READER_MODEL and score EM/F1"
    )
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--out", default="", help="write per-question rows as jsonl")
    ap.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Do not erase this run's uuid-minted musique-* namespaces at the end",
    )
    ap.add_argument(
        "--write-mode",
        choices=["set", "extract"],
        default="set",
        help="set = one memory.set per paragraph (recall@k scored from keys); extract = one "
        "memory.set_from_messages over all paragraphs (typed triples; no recall@k reported)",
    )
    ap.add_argument(
        "--namespace-salt",
        default="",
        help="deterministic namespace per (salt, question) so arms can share a written store",
    )
    ap.add_argument(
        "--skip-write",
        action="store_true",
        help="do not write; search the namespace a previous run with the same salt built",
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
