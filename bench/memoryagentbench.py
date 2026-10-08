"""MemoryAgentBench through loom — four competencies, one incremental protocol.

MemoryAgentBench defines four competencies — Accurate Retrieval (AR), Test-Time Learning
(TTL), Long-Range Understanding (LRU), Conflict Resolution / Selective Forgetting (CR) —
and one protocol: the context is cut into chunks (512 tokens for AR/CR, 4096 otherwise),
each chunk is memorized in turn, and every question is asked after the last chunk
("inject once, query many"). Multi-hop conflict resolution is its hardest split.

This harness runs the released dataset (ai-hyz/MemoryAgentBench, MIT; 146 contexts: AR
22, TTL 6, LRU 110, CR 8) through loom's HTTP ops with the same protocol, keeps the
reader fixed and reports memory-construction time, query time and tokens next to the
score.

Per context (fresh namespace):
  memorize   --memorize knowledge : each chunk → knowledge.add (verbatim,
                                    loom chunks + BM25 + vectors; the RAG
                                    shape of the benchmark's RAG rows)
             --memorize extract   : each chunk → memory.set_from_messages
                                    (facts extracted; the memory-agent shape)
  answer     --answer reader      : knowledge.search / memory.search top-k
                                    → fixed reader (BENCH_READER_MODEL)
             --answer ask         : memory.ask (loom's own synthesis;
                                    the facts-chain flag is measurable here)
  score      SubEM + F1 (AR, CR), label accuracy (TTL classification),
             Recall@5 over titles (TTL recsys), token-F1 vs the reference
             (LRU summary — the benchmark judges with gpt-4o; ours is a
             proxy and is labelled as such), SubEM (LRU QA).

Data: `--data` is a JSONL export of the HF dataset (one row per context:
context, questions, answers, metadata, split) or a parquet file when
pyarrow is importable. Export once with `datasets`:

    from datasets import load_dataset
    ds = load_dataset("ai-hyz/MemoryAgentBench")
    with open("mab.jsonl", "w") as f:
        for split in ds:
            for r in ds[split]:
                r["split"] = split; f.write(json.dumps(r) + "\\n")

Run:
    python bench/memoryagentbench.py --data mab.jsonl --splits Conflict_Resolution \\
        --base-url http://localhost:7789 --memorize extract --answer ask --out cr.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import os
import re
import string
import sys
import time
import uuid

try:
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        register_body,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        register_body,
        remember_bench_namespace,
    )
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Competencies and protocol constants (as the benchmark defines them)
# ---------------------------------------------------------------------------

SPLITS: dict[str, str] = {
    "Accurate_Retrieval": "AR",
    "Test_Time_Learning": "TTL",
    "Long_Range_Understanding": "LRU",
    "Conflict_Resolution": "CR",
}
# 512 tokens for AR and CR (and LongMemEval), 4096 for the rest.
CHUNK_TOKENS: dict[str, int] = {"AR": 512, "CR": 512, "TTL": 4096, "LRU": 4096}


@functools.lru_cache(maxsize=1)
def _encoder() -> Any:
    """The gpt-4o-mini tiktoken encoder, or None when tiktoken is unavailable."""
    try:
        import tiktoken  # type: ignore

        return tiktoken.encoding_for_model("gpt-4o-mini")
    except Exception:  # noqa: BLE001 — any import or model-table failure means the fallback
        return None


def approx_tokens(text: str) -> int:
    """tiktoken when importable (the benchmark counts gpt-4o-mini tokens), else
    ~4 characters per token. The summary's `token_counter` says which."""
    enc = _encoder()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text, disallowed_special=()))


def chunk_text(text: str, chunk_tokens: int) -> list[str]:
    """Sentence-aware chunks of about `chunk_tokens` tokens, as
    chunk_text_into_sentences does in the released harness."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for sent in sentences:
        if not sent:
            continue
        t = approx_tokens(sent)
        if buf and buf_tokens + t > chunk_tokens:
            chunks.append(" ".join(buf))
            buf, buf_tokens = [], 0
        buf.append(sent)
        buf_tokens += t
    if buf:
        chunks.append(" ".join(buf))
    return chunks


# ---------------------------------------------------------------------------
# Scoring — the released harness's normalisation (eval_other_utils.py)
# ---------------------------------------------------------------------------

_ARTICLES = re.compile(r"\b(a|an|the)\b")


def normalize_answer(s: str) -> str:
    s = (s or "").lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = _ARTICLES.sub(" ", s)
    return " ".join(s.split())


def sub_em(prediction: str, golds: list[str]) -> float:
    p = normalize_answer(prediction)
    for g in golds:
        ng = normalize_answer(g)
        if ng and ng in p:
            return 1.0
    return 0.0


def f1(prediction: str, golds: list[str]) -> float:
    p_tokens = normalize_answer(prediction).split()
    best = 0.0
    for g in golds:
        g_tokens = normalize_answer(g).split()
        common = Counter(p_tokens) & Counter(g_tokens)
        overlap = sum(common.values())
        if overlap == 0:
            continue
        prec, rec = overlap / len(p_tokens), overlap / len(g_tokens)
        best = max(best, 2 * prec * rec / (prec + rec))
    return best


def label_accuracy(prediction: str, gold: str) -> float:
    """TTL classification: the predicted label is the gold label when the
    normalised gold appears in the normalised prediction (the harness's
    `_process_icl_dataset` shape)."""
    return sub_em(prediction, [gold])


def recall_at_k(prediction: str, gold_titles: list[str], k: int = 5) -> float:
    """TTL recommendation: fraction of gold titles among the first k
    numbered lines of the prediction."""
    lines = [
        re.sub(r"^\s*\d+[.)]\s*", "", ln).strip() for ln in prediction.splitlines() if ln.strip()
    ]
    top = {normalize_answer(x) for x in lines[:k]}
    golds = [normalize_answer(g) for g in gold_titles if g]
    if not golds:
        return 0.0
    return sum(1 for g in golds if g in top) / len(golds)


# Loom cites its sources inline as `[memory_key]` / `[chunk:…]`; the markers are
# provenance, not answer words, and token F1 would count them as wrong tokens.
_CITATION_RE = re.compile(r"\[[^\]\n]+\]")


def strip_citations(prediction: str) -> str:
    return _CITATION_RE.sub(" ", prediction or "")


def score(competency: str, task: str, prediction: str, gold: Any) -> dict[str, float]:
    golds = gold if isinstance(gold, list) else [str(gold)]
    prediction = strip_citations(prediction)
    if competency == "TTL":
        if task == "recsys":
            return {"recall@5": recall_at_k(prediction, [str(g) for g in golds])}
        return {"accuracy": label_accuracy(prediction, str(golds[0]))}
    if competency == "LRU" and task == "summary":
        return {"token_f1_proxy": f1(prediction, [str(g) for g in golds])}
    gs = [str(g) for g in golds]
    return {"sub_em": sub_em(prediction, gs), "f1": f1(prediction, gs)}


def task_of(row: dict[str, Any]) -> str:
    meta = row.get("metadata") or {}
    src = str(meta.get("source") or row.get("source") or "").lower()
    if "rec" in src or "movie" in src:
        return "recsys"
    if "sum" in src:
        return "summary"
    if "icl" in src or any(k in src for k in ("banking", "clinc", "nlu", "trec")):
        return "icl"
    if "eventqa" in src or "event" in src:
        return "eventqa"
    if "longmemeval" in src or "lme" in src:
        return "longmemeval"
    return "qa"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def load_rows(
    path: Path, splits: list[str], max_contexts: int, source_regex: str = ""
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq  # type: ignore

        table = pq.read_table(path)
        for r in table.to_pylist():
            rows.append(r)
    else:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    out: list[dict[str, Any]] = []
    src_re = re.compile(source_regex) if source_regex else None
    for r in rows:
        split = str(r.get("split") or "")
        if splits and split not in splits:
            continue
        if src_re is not None:
            source = str((r.get("metadata") or {}).get("source") or r.get("source") or "")
            if not src_re.search(source):
                continue
        r["competency"] = SPLITS.get(split, "AR")
        r["task"] = task_of(r)
        out.append(r)
    return out[: max_contexts or len(out)]


def pairs_of(row: dict[str, Any]) -> list[tuple[str, Any]]:
    qs = row.get("questions") or []
    ans = row.get("answers") or []
    return list(zip(qs, ans, strict=False))


# ---------------------------------------------------------------------------
# Loom client
# ---------------------------------------------------------------------------


# Retries the harness had to make (429 / 5xx), by op. A build time is only
# a storage cost when this is 0; otherwise it is the server's rate limiter
# The FactConsolidation corpus states every relation as a function of its
# subject ("The author of X is Y", "X is a citizen of Y", "X is married to
# Y"); a later statement about the same subject is an update, not a second
# value. Loom's facts chain reads the current value only for
# relations the namespace declares single-valued, so the
# recipe declares them. Names are the extractor's stemmed relation verbs.
FC_ONTOLOGY: dict[str, str] = {
    verb: "one"
    for verb in (
        "citizen_of",
        "associ_with_sport",
        "find_by",
        "found_by",
        "author_of",
        "perform_by",
        "die_in",
        "locat_in",
        "affili_with",
        "marri_to",
        "spous_of",
        "play_position",
        "educ_at",
        "capit_of",
        "ceo_of",
        "creat_by",
        "creat_in",
        "chairperson_of",
        "born_in",
        "head_of_state_of",
        "head_of",
        "work_in",
        "famous_for",
        "offici_languag_of",
        "offici_language",
        "headquart_in",
        "director_of",
        "develop_by",
        "child_of",
        "religion_of",
        "profess",
        "job_titl",
        "posit",
    )
}

# (knowledge.add: burst 10, 2/s per namespace) pacing the incremental protocol.
RETRIES: dict[str, int] = {}


async def _post(client: Any, url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: Any = None
    for attempt in range(6):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=300.0)
        except httpx.TransportError:  # "Server disconnected": not an answer, retry twice
            if attempt >= 2:
                raise
            await asyncio.sleep(retry_delay(None, attempt))
            continue
        # 5xx and 429 both retry with backoff: the protocol memorizes once, so
        # one dropped chunk is one lost context. A Retry-After header is obeyed;
        # loom's write limiter answers with x-ratelimit-reset (seconds) instead.
        if r.status_code < 500 and r.status_code != 429:
            r.raise_for_status()
            return r.json()
        last = r
        reset = r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset")
        op = url.rsplit("/", 1)[-1]
        RETRIES[op] = RETRIES.get(op, 0) + 1
        await asyncio.sleep(retry_delay(reset, attempt))
    if last is not None:
        last.raise_for_status()
    raise RuntimeError("unreachable")


def retry_delay(retry_after: str | None, attempt: int) -> float:
    """Seconds to wait: the server's Retry-After when it is a number (capped
    at 60), else exponential backoff from 0.5 s."""
    try:
        if retry_after is not None and str(retry_after).strip():
            return min(60.0, max(0.0, float(str(retry_after).strip())))
    except ValueError:
        pass
    return min(60.0, 0.5 * (2**attempt))


_MEMORIZE_TURN = (
    "The following context is something I have read. Please memorize it; "
    "I will ask questions later.\n\n"
)

_READER = {
    "qa": (
        "Answer the question based on the memorized documents. Only give me the answer "
        "and do not output any other words.\n\nQuestion: {q}\n\nAnswer:"
    ),
    "icl": (
        "Based on the labelled examples you memorized, output only the label for the "
        "following input.\n\n{q}\n\nLabel:"
    ),
    "recsys": (
        "Pretend you are a movie recommender system. Based on the dialogues you memorized, "
        "reply with 20 recommendations, one per numbered line, no extra sentences.\n\n"
        "Conversation: {q}\n\nThe recommendations are:\n"
    ),
    "summary": "Based on the book you memorized, {q}",
    "cr": (
        "Pretend you are a knowledge management system. Each fact in the knowledge pool has "
        "a serial number and the newer fact has the larger serial number; resolve conflicts "
        "by taking the newest fact. Answer only from the knowledge pool, very concisely."
        "\n\nQuestion: {q}\nAnswer:"
    ),
}


def reader_prompt(competency: str, task: str, question: str, passages: list[str]) -> str:
    key = "cr" if competency == "CR" else task if task in _READER else "qa"
    ctx = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(passages))
    return f"Memorized documents:\n{ctx}\n\n" + _READER[key].format(q=question)


async def _read(
    client: Any, prompt: str, model: str, api_key: str, max_tokens: int
) -> tuple[str, int, int]:
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    r = await client.post(
        base + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120.0,
    )
    r.raise_for_status()
    payload = r.json()
    usage = payload.get("usage") or {}
    return (
        (payload["choices"][0]["message"]["content"] or "").strip(),
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )


async def run_context(
    client: Any,
    row: dict[str, Any],
    *,
    base_url: str,
    token: str,
    org: str,
    memorize: str,
    answer: str,
    top_k: int,
    reader_model: str,
    api_key: str,
    ask_json: dict[str, Any],
    max_questions: int,
    chunk_override: int,
    namespace_json: dict[str, Any] | None = None,
    bulk_chunks: int = 1,
    ontology_json: dict[str, str] | None = None,
    knowledge_json: dict[str, Any] | None = None,
    use_span: bool = False,
    namespace_salt: str = "",
    skip_memorize: bool = False,
    coverage_sweep: bool = True,
    keep: bool = False,
) -> list[dict[str, Any]]:
    comp, task = row["competency"], row["task"]
    # namespace_salt: the same salt and context give the same namespace, so a
    # second arm (another search mode, spans on) can re-read a memorized
    # namespace with skip_memorize instead of paying the build again.
    ctx = str(row.get("context") or "")
    if namespace_salt:
        seed = f"{namespace_salt}|{row.get('id', '')}|{ctx[:2000]}"
        digest = hashlib.sha1(seed.encode()).hexdigest()
        ns = f"mab-{comp.lower()}-{digest[:8]}"
    else:
        ns = f"mab-{comp.lower()}-{uuid.uuid4().hex[:8]}"
        remember_bench_namespace(org, ns)  # throwaway: erased when the run exits
    identity = {"org": org, "namespace": ns, "agent": "mab-bench", "user_id": "-"}
    if not skip_memorize:
        # Registered as bench before the first write; a salted store is written
        # for other arms to re-read, so it is kept. --namespace-json settings
        # (e.g. default_conflict_policy=newest_wins) act at write time, so a
        # refused registration then fails the context instead of measuring
        # without them.
        post = lambda p, b: _post(client, base_url + p, b, token)  # noqa: E731
        keep_ns = keep or bool(namespace_salt)
        if namespace_json:
            await post(
                "/v1/namespace.register",
                register_body(
                    org,
                    ns,
                    description=f"MemoryAgentBench {comp} context",
                    keep=keep_ns,
                    extra=namespace_json,
                ),
            )
        else:
            await register_bench_namespace(
                post,
                org=org,
                namespace=ns,
                description=f"MemoryAgentBench {comp} context",
                keep=keep_ns,
            )
    for verb, cardinality in (ontology_json or {}).items():
        # The corpus's own cardinalities: FactConsolidation
        # edits `author_of` and `founded_by` as functional relations, which
        # the seed list keeps multi-valued; the namespace says otherwise here.
        await _post(
            client,
            base_url + "/v1/namespace.register_ontology",
            {
                "org": org,
                "namespace": ns,
                "kind": "relation_verb",
                "name": verb,
                "cardinality": cardinality,
            },
            token,
        )
    chunk_tokens = chunk_override or CHUNK_TOKENS[comp]
    chunks = chunk_text(ctx, chunk_tokens)
    if skip_memorize:
        chunks_to_write: list[str] = []
    else:
        chunks_to_write = chunks
    t0 = time.perf_counter()
    build_tokens = 0
    retries_before = sum(RETRIES.values())
    if memorize == "knowledge" and bulk_chunks > 1:
        # Verbatim store: the rows are the same whether the chunks arrive one
        # per call or `bulk_chunks` per knowledge.bulk_add. The incremental
        # protocol exists for systems that summarise as they go; here it only
        # meets the per-call rate limiter. One call per batch, in order.
        for start in range(0, len(chunks_to_write), bulk_chunks):
            batch = chunks_to_write[start : start + bulk_chunks]
            build_tokens += sum(approx_tokens(c) for c in batch)
            await _post(
                client,
                base_url + "/v1/knowledge.bulk_add",
                {
                    "org": identity["org"],
                    "namespace": identity["namespace"],
                    "entries": [
                        {
                            "source_uri": f"mab://{ns}/chunk-{start + j:05d}",
                            "content": c,
                            "title": f"chunk {start + j}",
                            "tags": ["mab", comp.lower()],
                        }
                        for j, c in enumerate(batch)
                    ],
                },
                token,
            )
        chunks_iter: list[str] = []
    else:
        chunks_iter = chunks_to_write
    for i, chunk in enumerate(chunks_iter):
        build_tokens += approx_tokens(chunk)
        if memorize == "knowledge":
            await _post(
                client,
                base_url + "/v1/knowledge.add",
                {
                    **identity,
                    "source_uri": f"mab://{ns}/chunk-{i:05d}",
                    "content": chunk,
                    "title": f"chunk {i}",
                    "tags": ["mab", comp.lower()],
                },
                token,
            )
        else:
            await _post(
                client,
                base_url + "/v1/memory.set_from_messages",
                {
                    **identity,
                    "session_id": f"chunk-{i:05d}",
                    "messages": [
                        {"role": "user", "content": _MEMORIZE_TURN + chunk},
                        {"role": "assistant", "content": "I have memorized it."},
                    ],
                    "max_tokens": 4096,
                    # A fact list is statement-dense: ask for the second
                    # reading unless told otherwise.
                    **({"coverage_sweep": True} if coverage_sweep else {}),
                },
                token,
            )
    build_ms = int((time.perf_counter() - t0) * 1000)
    build_retries = sum(RETRIES.values()) - retries_before
    rows: list[dict[str, Any]] = []
    for qi, (question, gold) in enumerate(pairs_of(row)[: max_questions or None]):
        t1 = time.perf_counter()
        pred, in_tok, out_tok, hits_n = "", 0, 0, 0
        if answer == "ask":
            resp = await _post(
                client,
                base_url + "/v1/memory.ask",
                {
                    **identity,
                    # memory.ask caps the query at 2,000 characters; the summary
                    # task's question carries a few-shot demo after the instruction.
                    "query": str(question)[:2000],
                    "top_k": top_k,
                    # Each question lives in its own namespace; a wildcard token
                    # would otherwise fan the ask out across every namespace the
                    # token can read (other runs' facts joined the pool).
                    "namespace_scope": "this",
                    **ask_json,
                },
                token,
            )
            pred = str(resp.get("answer") or "")
            hits_n = len(resp.get("results") or [])
        else:
            if memorize == "knowledge":
                resp = await _post(
                    client,
                    base_url + "/v1/knowledge.search",
                    {**identity, "query": str(question)[:2000], "top_k": top_k, **knowledge_json},
                    token,
                )
                # use_span arm: read the hit's sentence window
                # when the server narrowed one, else the chunk.
                passages = [
                    str((h.get("span") if use_span else "") or h.get("content") or "")
                    for h in resp.get("results") or []
                ]
            else:
                resp = await _post(
                    client,
                    base_url + "/v1/memory.search",
                    {
                        **identity,
                        "query": str(question)[:2000],
                        "top_k": top_k,
                        "namespace_scope": "this",
                    },
                    token,
                )
                passages = [str(h.get("content_excerpt") or "") for h in resp.get("results") or []]
            hits_n = len(passages)
            if reader_model and api_key:
                max_tokens = 1500 if task == "summary" else 200
                prompt = reader_prompt(comp, task, str(question), passages)
                pred, in_tok, out_tok = await _read(
                    client, prompt, reader_model, api_key, max_tokens
                )
        query_ms = int((time.perf_counter() - t1) * 1000)
        rows.append(
            {
                "split": row.get("split"),
                "competency": comp,
                "task": task,
                "context_id": row.get("id", ns),
                "question_idx": qi,
                "prediction": pred[:2000],
                "hits": hits_n,
                **score(comp, task, pred, gold),
                "build_ms": build_ms,
                "build_tokens": build_tokens,
                "chunks": len(chunks),
                "build_retries": build_retries,
                "bulk_chunks": bulk_chunks if memorize == "knowledge" else 1,
                "chunk_tokens": chunk_tokens,
                "query_ms": query_ms,
                "reader_in_tokens": in_tok,
                "reader_out_tokens": out_tok,
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n_questions": len(rows)}
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["competency"], []).append(r)
    for comp, rs in sorted(by.items()):
        metrics: dict[str, float] = {}
        for key in ("sub_em", "f1", "accuracy", "recall@5", "token_f1_proxy"):
            vals = [r[key] for r in rs if key in r]
            if vals:
                metrics[key] = round(sum(vals) / len(vals), 4)
        ctxs = {r["context_id"]: r for r in rs}
        out[comp] = {
            "n": len(rs),
            **metrics,
            "build_ms_per_context_p50": sorted(c["build_ms"] for c in ctxs.values())[
                len(ctxs) // 2
            ],
            "query_ms_p50": sorted(r["query_ms"] for r in rs)[len(rs) // 2],
            "chunks_per_context_mean": round(
                sum(c["chunks"] for c in ctxs.values()) / len(ctxs), 1
            ),
        }
    return out


async def main_async(args: argparse.Namespace) -> int:

    if args.memorize == "knowledge" and args.answer == "ask" and not args.allow_knowledge_ask:
        # memory.ask reads the memory plane; a namespace filled only through
        # knowledge.add has no memory rows and every ask abstains (measured:
        # 200/200 empty on the conflict split). Refuse rather than score zero.
        # --allow-knowledge-ask is the arm where the server's synthesize route
        # reads the knowledge plane for summary-shaped questions.
        print(
            "--memorize knowledge cannot be answered with --answer ask: memory.ask does not "
            "read the knowledge plane. Use --answer reader (knowledge.search + reader), "
            "--memorize extract, or --allow-knowledge-ask (summary-shaped questions take "
            "the synthesize route).",
            file=sys.stderr,
        )
        return 2
    splits = [s for s in (args.splits.split(",") if args.splits else []) if s]
    rows_in = load_rows(Path(args.data), splits, args.max_contexts, args.source_regex)
    if not rows_in:
        print("no contexts loaded", file=sys.stderr)
        return 1
    api_key = os.environ.get("OPENAI_API_KEY", "") if args.answer == "reader" else ""
    if args.answer == "reader" and not api_key:
        # Without a key the reader never runs and every prediction scores 0.
        print("--answer reader needs OPENAI_API_KEY", file=sys.stderr)
        return 2
    reader_model = (
        (os.environ.get("BENCH_READER_MODEL") or "gpt-4o-mini") if args.answer == "reader" else ""
    )
    ask_json = json.loads(args.ask_json) if args.ask_json else {}
    knowledge_json = json.loads(args.knowledge_json) if args.knowledge_json else {}
    namespace_json = json.loads(args.namespace_json) if args.namespace_json else {}
    ontology_json = json.loads(args.ontology_json) if args.ontology_json else {}
    if getattr(args, "ontology_preset", "") == "fc":
        ontology_json = {**FC_ONTOLOGY, **(ontology_json or {})}
    results: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=300.0) as client:

        async def _one(row: dict[str, Any]) -> None:
            async with sem:
                try:
                    results.extend(
                        await run_context(
                            client,
                            row,
                            base_url=args.base_url.rstrip("/"),
                            token=args.token,
                            org=args.org,
                            memorize=args.memorize,
                            answer=args.answer,
                            top_k=args.top_k,
                            reader_model=reader_model,
                            api_key=api_key,
                            ask_json=ask_json,
                            max_questions=args.max_questions,
                            chunk_override=args.chunk_tokens,
                            namespace_json=namespace_json,
                            coverage_sweep=not args.no_coverage_sweep,
                            bulk_chunks=args.bulk_chunks,
                            ontology_json=ontology_json,
                            knowledge_json=knowledge_json,
                            use_span=args.use_span,
                            namespace_salt=args.namespace_salt,
                            skip_memorize=args.skip_memorize,
                            keep=args.keep_namespaces,
                        )
                    )
                except Exception as e:  # noqa: BLE001 — one context must not sink the run
                    results.append(
                        {
                            "split": row.get("split"),
                            "competency": row["competency"],
                            "task": row["task"],
                            "context_id": row.get("id", "?"),
                            "question_idx": -1,
                            "hits": 0,
                            # Type name first: an httpx timeout stringifies to "".
                            "error": f"{type(e).__name__}: {e}"[:200],
                            "build_ms": 0,
                            "build_tokens": 0,
                            "chunks": 0,
                            "chunk_tokens": 0,
                            "query_ms": 0,
                        }
                    )

        # Uuid-minted namespaces are erased on exit, after a failure or Ctrl-C
        # too; salted ones were never remembered and stay.
        async with erase_on_exit(
            lambda p, b: _post(client, args.base_url.rstrip("/") + p, b, args.token),
            org=args.org,
            keep=args.keep_namespaces,
        ):
            await asyncio.gather(*[_one(r) for r in rows_in])
    summary = {
        "dataset": "MemoryAgentBench",
        "memorize": args.memorize,
        "answer": args.answer,
        "splits": splits,
        "source_regex": args.source_regex or None,
        "ask_json": ask_json,
        "namespace_json": namespace_json,
        "ontology_json": ontology_json,
        "knowledge_json": knowledge_json,
        "use_span": args.use_span,
        "reader": reader_model or None,
        "top_k": args.top_k,
        "token_counter": "tiktoken" if _encoder() is not None else "chars/4",
        "errors": sum(1 for r in results if "error" in r),
        **summarize([r for r in results if "error" not in r]),
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r) for r in results))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data", required=True, help="JSONL export (with a `split` field) or parquet")
    ap.add_argument("--splits", default="", help="comma list of HF splits; default all four")
    ap.add_argument("--max-contexts", type=int, default=0)
    ap.add_argument(
        "--source-regex",
        default="",
        help="only contexts whose metadata.source matches this regex",
    )
    ap.add_argument("--max-questions", type=int, default=0, help="per context; 0 = all")
    ap.add_argument(
        "--chunk-tokens", type=int, default=0, help="override the per-competency chunk size"
    )
    ap.add_argument("--memorize", choices=("knowledge", "extract"), default="knowledge")
    ap.add_argument(
        "--bulk-chunks",
        type=int,
        default=1,
        help="knowledge path: chunks per knowledge.bulk_add call (1 = one knowledge.add per "
        "chunk, the benchmark's incremental protocol; >1 batches a verbatim store and side-steps "
        "the per-call rate limiter). Reported per row as bulk_chunks / build_retries.",
    )
    ap.add_argument("--answer", choices=("reader", "ask"), default="reader")
    ap.add_argument("--ask-json", default="", help="extra memory.ask fields for --answer ask")
    ap.add_argument(
        "--knowledge-json",
        default="",
        help="extra knowledge.search fields for --answer reader, e.g. '{\"narrow_spans\": true}'",
    )
    ap.add_argument(
        "--use-span",
        action="store_true",
        help="feed the reader each hit's `span` instead of its chunk when present",
    )
    ap.add_argument(
        "--namespace-salt",
        default="",
        help="deterministic namespace per (salt, context) so arms can share a memorized store",
    )
    ap.add_argument(
        "--skip-memorize",
        action="store_true",
        help="do not write; answer against the namespace a previous run with the same salt built",
    )
    ap.add_argument(
        "--allow-knowledge-ask",
        action="store_true",
        help="permit --memorize knowledge --answer ask "
        "(server has the knowledge synthesize route on)",
    )
    ap.add_argument(
        "--namespace-json",
        default="",
        help="namespace.register fields for each fresh namespace, e.g. "
        '\'{"default_conflict_policy": "newest_wins"}\'',
    )
    ap.add_argument(
        "--ontology-json",
        default="",
        help="relation cardinalities to declare on each fresh namespace via "
        "namespace.register_ontology, e.g. "
        '\'{"author_of":"one","founded_by":"one"}\'',
    )
    ap.add_argument(
        "--ontology-preset",
        choices=("", "fc"),
        default="",
        help="'fc': declare the FactConsolidation corpus's relations single-valued "
        "(FC_ONTOLOGY below) — every one of its templated facts is functional, and "
        "the facts chain reads the current value only for declared relations. "
        "Measured on factconsolidation_mh_6k, 20 questions: SubEM 0.20 → 0.40.",
    )
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="dev")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument(
        "--no-coverage-sweep",
        action="store_true",
        help="do not ask memory.set_from_messages for the coverage sweep; "
        "by default every chunk asks for it, these contexts being statement-dense",
    )
    ap.add_argument("--out", default="")
    ap.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Do not erase this run's uuid-minted mab-* namespaces at the end",
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
