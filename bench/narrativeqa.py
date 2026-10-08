"""NarrativeQA through loom's knowledge plane — long-document QA.

NarrativeQA: 1,572 books and film scripts with ~30 free-form questions each,
written from a summary and answered against the full story (tens of
thousands of words). It is the long-document arm next to LoCoMo's
conversations, and a narrative is the corpus where event order matters most.

Loom's long-document store is the knowledge plane — sections, outlines,
hybrid BM25 + vectors. This harness gives it a long-narrative number in the
published shape: F1 and ROUGE-L against both reference answers (best of the
two), plus an optional judge, plus build time, tokens per question and search
latency.

Per document (fresh namespace, `bench-nqa-<doc id>-<salt>`):
  1. the full text is cut into paragraph-aligned chunks of `--chunk-tokens`
     (default 1,000) and written with `knowledge.bulk_add` (verbatim; loom
     sections, chunks, embeds and indexes it);
  2. every question runs `knowledge.search` (top-k 12 — measured +5 judged points
     over 8 on the sound documents at +50% reader tokens; `--search-mode`) and a
     fixed reader (BENCH_READER_MODEL, default gpt-4.1-mini) answers from
     the passages only;
  3. scored: token F1 and ROUGE-L (F) vs. the better of the two gold
     answers, SQuAD normalisation; `--judge` adds a CORRECT/WRONG label
     from BENCH_JUDGE_MODEL (default gpt-4.1) — the accuracy loom's other
     benches report, and the number F1 undercounts (a correct answer worded
     differently scores 0.3 F1).

Data: the HF test split through the datasets server (no login, no
`datasets` install):

    python bench/narrativeqa.py --fetch 10 --data nqa10.jsonl
    # one line per document: {id, kind, title, text, questions:[{q, answers}]}

Run:
    OPENAI_API_KEY=... python bench/narrativeqa.py --data nqa10.jsonl \\
        --base-url http://localhost:7789 --token ... --out nqa.jsonl --judge

Cost: ten documents ≈ 500k words in, ~300 reader calls (and ~300 judge
calls with --judge); a few dollars. The run's namespaces are erased when it
exits, after a failure too, unless `--keep-namespaces` or `--namespace-salt`
keeps them for a later `--skip-memorize` run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import statistics
import sys
import time
import uuid
from typing import Any

try:
    from bench.answer_scoring import rouge_l, token_f1
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
    from bench.memoryagentbench import _post, _read, approx_tokens, chunk_text
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from answer_scoring import rouge_l, token_f1  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
    from memoryagentbench import _post, _read, approx_tokens, chunk_text  # type: ignore[no-redef]

ROWS_URL = "https://datasets-server.huggingface.co/rows"
DATASET = "deepmind/narrativeqa"
_DEFAULT_READER_MODEL = os.environ.get("BENCH_READER_MODEL", "gpt-4.1-mini")
_DEFAULT_JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "gpt-4.1")

_READER_PROMPT = (
    "You are answering a question about a story from retrieved passages of it.\n"
    "Answer in one short phrase or sentence, using only the passages. If the passages "
    "do not contain the answer, reply exactly: I don't know.\n\n"
    "Passages:\n{ctx}\n\nQuestion: {q}\nAnswer:"
)
_JUDGE_PROMPT = (
    "Grade an answer to a question about a story. Reply with exactly one word, "
    "CORRECT or WRONG. CORRECT when the answer states the same fact as either "
    "reference (wording may differ; extra correct detail is fine). WRONG when it "
    "contradicts them, names a different entity, or says it does not know.\n\n"
    "Question: {q}\nReference answers: {a1} | {a2}\nAnswer: {pred}\nGrade:"
)


# ---------------------------------------------------------------------------
# Dataset: rows → documents
# ---------------------------------------------------------------------------


def group_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Datasets-server rows (one per question) → {document id: document with
    its questions}. Order of first appearance is kept (dict order)."""
    docs: dict[str, dict[str, Any]] = {}
    for r in rows:
        row = r.get("row", r)
        doc = row.get("document") or {}
        did = str(doc.get("id") or "")
        if not did:
            continue
        d = docs.setdefault(
            did,
            {
                "id": did,
                "kind": str(doc.get("kind") or ""),
                "title": str(((doc.get("summary") or {}).get("title")) or ""),
                "words": int(doc.get("word_count") or 0),
                "text": str(doc.get("text") or ""),
                "questions": [],
            },
        )
        q = str(((row.get("question") or {}).get("text")) or "").strip()
        answers = [str(a.get("text") or "").strip() for a in (row.get("answers") or [])]
        answers = [a for a in answers if a]
        if q and answers:
            d["questions"].append({"q": q, "answers": answers})
    return docs


async def fetch_documents(
    client: Any, n_docs: int, *, split: str = "test", page: int = 100, max_pages: int = 400
) -> list[dict[str, Any]]:
    """The first `n_docs` documents of the split with all their questions.
    Rows for one document are contiguous in the split, so paging until the
    (n_docs + 1)-th document appears makes the first n complete."""
    seen: list[dict[str, Any]] = []
    docs: dict[str, dict[str, Any]] = {}
    for i in range(max_pages):
        params = {
            "dataset": DATASET,
            "config": "default",
            "split": split,
            "offset": i * page,
            "length": page,
        }
        r = await client.get(ROWS_URL, params=params, timeout=120.0)
        r.raise_for_status()
        rows = r.json().get("rows") or []
        if not rows:
            break
        seen.extend(rows)
        docs = group_rows(seen)
        if len(docs) > n_docs:
            break
    return list(docs.values())[:n_docs]


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def strip_html(text: str) -> str:
    """Film scripts in NarrativeQA are IMSDb pages: HTML with <b>, <tt>,
    <font> around every line. Tags out, entities decoded, runs of blank
    lines folded, so a chunk is script, not markup."""
    if "<" not in text:
        return text
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
    t = _TAG_RE.sub("", t)
    t = html.unescape(t)
    t = "\n".join(_WS_RE.sub(" ", ln).rstrip() for ln in t.splitlines())
    return _BLANKS_RE.sub("\n\n", t).strip()


def title_in_text(title: str, text: str) -> bool:
    """Whether the document text mentions its own title (the part before any
    parenthetical). NarrativeQA's source URLs sometimes fetched the wrong
    work — a 'Mrs. Frisby' entry whose text is a Gutenberg pamphlet on house
    rats — and a false here flags the document in the per-document table."""
    core = re.sub(r"\s*\(.*?\)\s*$", "", title or "").strip().lower()
    core = re.sub(r"^(the|a|an)\s+", "", core)
    return bool(core) and core in (text or "").lower()


def load_docs(path: str) -> list[dict[str, Any]]:
    """Documents from the JSONL, HTML stripped, with `title_match` set."""
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            d["text"] = strip_html(str(d.get("text") or ""))
            d["title_match"] = title_in_text(str(d.get("title") or ""), d["text"])
            out.append(d)
    return out


def namespace_for(doc_id: str, salt: str) -> str:
    """Deterministic per (document, salt): a second arm re-reads the same
    namespace with --skip-memorize instead of paying the build again."""
    digest = hashlib.sha1(f"{salt}|{doc_id}".encode()).hexdigest()
    return f"bench-nqa-{doc_id[:8]}-{digest[:6]}"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def best_f1(prediction: str, answers: list[str]) -> float:
    return max((token_f1(prediction, a, normalize="squad") for a in answers), default=0.0)


def best_rouge_l(prediction: str, answers: list[str]) -> float:
    """ROUGE-L F1 (`rouge_l` returns (F1, recall)) against the better gold answer."""
    return max((rouge_l(prediction, a, normalize="squad")[0] for a in answers), default=0.0)


def parse_grade(text: str) -> bool | None:
    t = (text or "").strip().upper()
    if t.startswith("CORRECT"):
        return True
    if t.startswith("WRONG"):
        return False
    return None


# A document the reader abstains on almost every time is not a hard document,
# it is the wrong document: NarrativeQA's source URLs sometimes fetched another
# work (a 'Mrs. Frisby' whose text is a pamphlet on house rats; an 'Up in the
# Air (1940)' whose text is the 2009 script). The per-document table flags them
# and the summary reports the sound documents separately.
SUSPECT_ABSTAIN_RATE = 0.9


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-run and per-document means; judged accuracy only when a judge ran;
    documents whose source text is suspect (title absent from the text, or
    abstention on >= 90% of questions) flagged and excluded from the `_clean`
    figures."""
    scored = [r for r in rows if not r.get("error")]
    out: dict[str, Any] = {
        "questions": len(rows),
        "errors": len(rows) - len(scored),
        "documents": len({r["doc_id"] for r in rows}),
        "f1": round(statistics.fmean([r["f1"] for r in scored]) * 100, 2) if scored else 0.0,
        "rouge_l": round(statistics.fmean([r["rouge_l"] for r in scored]) * 100, 2)
        if scored
        else 0.0,
        "abstain_rate": round(
            statistics.fmean([1.0 if r.get("abstained") else 0.0 for r in scored]) * 100, 2
        )
        if scored
        else 0.0,
        "reader_tokens_per_q": round(
            statistics.fmean([r.get("reader_tokens_in", 0) for r in scored])
        )
        if scored
        else 0,
        "search_p50_ms": round(statistics.median([r.get("search_ms", 0) for r in scored]))
        if scored
        else 0,
    }
    judged = [r for r in scored if r.get("judged") is not None]
    if judged:
        out["judged_n"] = len(judged)
        out["judged_acc"] = round(
            statistics.fmean([1.0 if r["judged"] else 0.0 for r in judged]) * 100, 2
        )
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for r in scored:
        by_doc.setdefault(r["doc_id"], []).append(r)

    def _abstain(rs: list[dict[str, Any]]) -> float:
        return _mean([1.0 if r.get("abstained") else 0.0 for r in rs])

    def _suspect(rs: list[dict[str, Any]]) -> bool:
        return (not bool(rs[0].get("title_match", True))) or _abstain(rs) >= SUSPECT_ABSTAIN_RATE

    out["per_document"] = {
        d: {
            "title": rs[0].get("title", ""),
            "title_match": bool(rs[0].get("title_match", True)),
            "suspect_source": _suspect(rs),
            "abstain_rate": round(_abstain(rs) * 100, 2),
            "n": len(rs),
            "f1": round(statistics.fmean([r["f1"] for r in rs]) * 100, 2),
            "rouge_l": round(statistics.fmean([r["rouge_l"] for r in rs]) * 100, 2),
            **(
                {
                    "judged_acc": round(
                        statistics.fmean(
                            [1.0 if r["judged"] else 0.0 for r in rs if r.get("judged") is not None]
                        )
                        * 100,
                        2,
                    )
                }
                if any(r.get("judged") is not None for r in rs)
                else {}
            ),
        }
        for d, rs in by_doc.items()
    }
    clean = [r for rs in by_doc.values() if not _suspect(rs) for r in rs]
    out["clean_documents"] = sum(1 for rs in by_doc.values() if not _suspect(rs))
    out["suspect_documents"] = len(by_doc) - out["clean_documents"]
    out["f1_clean"] = round(_mean([r["f1"] for r in clean]) * 100, 2)
    out["rouge_l_clean"] = round(_mean([r["rouge_l"] for r in clean]) * 100, 2)
    judged_clean = [r for r in clean if r.get("judged") is not None]
    if judged_clean:
        out["judged_acc_clean"] = round(
            _mean([1.0 if r["judged"] else 0.0 for r in judged_clean]) * 100, 2
        )
    return out


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def memorize_document(
    client: Any,
    doc: dict[str, Any],
    *,
    base_url: str,
    token: str,
    org: str,
    namespace: str,
    chunk_tokens: int,
    bulk: int = 50,
) -> dict[str, Any]:
    chunks = chunk_text(str(doc.get("text") or ""), chunk_tokens)
    t0 = time.perf_counter()
    for start in range(0, len(chunks), bulk):
        batch = chunks[start : start + bulk]
        await _post(
            client,
            base_url + "/v1/knowledge.bulk_add",
            {
                "org": org,
                "namespace": namespace,
                "entries": [
                    {
                        "source_uri": f"nqa://{doc['id']}/chunk-{start + j:04d}",
                        "content": c,
                        "title": f"{doc.get('title') or doc['id']} — part {start + j + 1}",
                        "tags": ["nqa", str(doc.get("kind") or "")],
                    }
                    for j, c in enumerate(batch)
                ],
            },
            token,
        )
    return {
        "chunks": len(chunks),
        "build_tokens": sum(approx_tokens(c) for c in chunks),
        "build_seconds": round(time.perf_counter() - t0, 1),
    }


async def answer_question(
    client: Any,
    doc: dict[str, Any],
    item: dict[str, Any],
    *,
    base_url: str,
    token: str,
    org: str,
    namespace: str,
    top_k: int,
    search_mode: str,
    reader_model: str,
    judge_model: str,
    api_key: str,
    search_json: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "doc_id": doc["id"],
        "title": doc.get("title", ""),
        "title_match": bool(doc.get("title_match", True)),
        "question": item["q"],
        "answers": item["answers"],
    }
    try:
        t0 = time.perf_counter()
        res = await _post(
            client,
            base_url + "/v1/knowledge.search",
            {
                "org": org,
                "namespace": namespace,
                "query": item["q"],
                "top_k": top_k,
                "search_mode": search_mode,
                "namespace_scope": "this",
                **search_json,
            },
            token,
        )
        out["search_ms"] = round((time.perf_counter() - t0) * 1000)
        hits = res.get("results") or []
        passages = [str(h.get("content") or "") for h in hits]
        out["hits"] = len(hits)
        ctx = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(passages)) or "(none)"
        pred, tin, tout = await _read(
            client, _READER_PROMPT.format(ctx=ctx, q=item["q"]), reader_model, api_key, 80
        )
        out["prediction"] = pred
        out["reader_tokens_in"], out["reader_tokens_out"] = tin, tout
        out["abstained"] = pred.strip().lower().startswith("i don't know")
        out["f1"] = best_f1(pred, item["answers"])
        out["rouge_l"] = best_rouge_l(pred, item["answers"])
        if judge_model:
            a = item["answers"]
            grade, _, _ = await _read(
                client,
                _JUDGE_PROMPT.format(
                    q=item["q"], a1=a[0], a2=a[1] if len(a) > 1 else a[0], pred=pred
                ),
                judge_model,
                api_key,
                3,
            )
            out["judged"] = parse_grade(grade)
    except Exception as e:  # noqa: BLE001 — one question's failure is one row
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out.setdefault("f1", 0.0)
        out.setdefault("rouge_l", 0.0)
    return out


async def run_document(
    client: Any, doc: dict[str, Any], args: argparse.Namespace, api_key: str
) -> list[dict[str, Any]]:
    salt = args.namespace_salt or uuid.uuid4().hex[:8]
    ns = namespace_for(str(doc["id"]), salt)
    base = args.base_url.rstrip("/")
    if not args.skip_memorize:
        # A salted namespace is built for a later --skip-memorize run, so it is kept.
        keep = args.keep_namespaces or bool(args.namespace_salt)
        if not keep:
            remember_bench_namespace(args.org, ns)
        await register_bench_namespace(
            lambda p, b: _post(client, base + p, b, args.token),
            org=args.org,
            namespace=ns,
            description=f"NarrativeQA {doc.get('kind', '')}: {doc.get('title', doc['id'])}",
            keep=keep,
        )
        build = await memorize_document(
            client,
            doc,
            base_url=base,
            token=args.token,
            org=args.org,
            namespace=ns,
            chunk_tokens=args.chunk_tokens,
        )
        print(
            f"[{doc.get('title') or doc['id']}] {build['chunks']} chunks, "
            f"{build['build_tokens']} tokens, {build['build_seconds']} s → {ns}",
            file=sys.stderr,
        )
    else:
        build = {"chunks": 0, "build_tokens": 0, "build_seconds": 0.0}
    items = list(doc.get("questions") or [])[: args.max_questions or None]
    sem = asyncio.Semaphore(args.qa_concurrency)
    search_json = json.loads(args.search_json) if args.search_json else {}

    async def one(item: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await answer_question(
                client,
                doc,
                item,
                base_url=base,
                token=args.token,
                org=args.org,
                namespace=ns,
                top_k=args.top_k,
                search_mode=args.search_mode,
                reader_model=args.reader_model,
                judge_model=args.judge_model if args.judge else "",
                api_key=api_key,
                search_json=search_json,
            )

    rows = await asyncio.gather(*[one(it) for it in items])
    for r in rows:
        r["namespace"] = ns
        r.update({f"build_{k}": v for k, v in build.items()})
    return list(rows)


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    if args.fetch:
        async with httpx.AsyncClient() as client:
            docs = await fetch_documents(client, int(args.fetch), split=args.split)
        with open(args.data, "w", encoding="utf-8") as f:
            for d in docs:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        n_q = sum(len(d["questions"]) for d in docs)
        print(f"fetched {len(docs)} documents, {n_q} questions → {args.data}")
        return 0
    api_key = os.environ.get("BENCH_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print(
            "OPENAI_API_KEY (or BENCH_OPENAI_API_KEY) is required for the reader", file=sys.stderr
        )
        return 2
    docs = load_docs(args.data)
    if args.limit_documents:
        docs = docs[: args.limit_documents]
    rows: list[dict[str, Any]] = []
    base = args.base_url.rstrip("/")
    async with (
        httpx.AsyncClient() as client,
        erase_on_exit(
            lambda p, b: _post(client, base + p, b, args.token),
            org=args.org,
            keep=args.keep_namespaces,
        ),
    ):
        for doc in docs:
            rows.extend(await run_document(client, doc, args, api_key))
    summary = summarize(rows)
    summary.update(
        {
            "reader": args.reader_model,
            "judge": args.judge_model if args.judge else None,
            "search_mode": args.search_mode,
            "top_k": args.top_k,
            "chunk_tokens": args.chunk_tokens,
            "build_seconds_total": round(
                sum({r["doc_id"]: r.get("build_build_seconds", 0) for r in rows}.values()), 1
            ),
        }
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(args.out + ".summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1, ensure_ascii=False)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_document"}, indent=1))
    for d, s in summary["per_document"].items():
        print(
            f"  {d[:8]} {s.get('title', '')[:40]:40s} n={s['n']:3d} F1={s['f1']:5.1f} "
            f"RL={s['rouge_l']:5.1f}"
            + (f" acc={s['judged_acc']:5.1f}" if "judged_acc" in s else "")
            + ("  [suspect source]" if s.get("suspect_source") else "")
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data", required=True, help="JSONL of documents (see --fetch)")
    ap.add_argument(
        "--fetch", type=int, default=0, help="fetch N test documents into --data and exit"
    )
    ap.add_argument("--split", default="test")
    ap.add_argument("--base-url", default="http://localhost:7789")
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="bench")
    ap.add_argument("--top-k", type=int, default=12)
    ap.add_argument("--search-mode", default="hybrid", choices=["hybrid", "semantic", "lexical"])
    ap.add_argument(
        "--search-json", default="", help="JSON merged into every knowledge.search body"
    )
    ap.add_argument("--chunk-tokens", type=int, default=1000)
    ap.add_argument("--reader-model", default=_DEFAULT_READER_MODEL)
    ap.add_argument(
        "--judge", action="store_true", help="also grade CORRECT/WRONG with --judge-model"
    )
    ap.add_argument("--judge-model", default=_DEFAULT_JUDGE_MODEL)
    ap.add_argument("--limit-documents", type=int, default=0)
    ap.add_argument("--max-questions", type=int, default=0, help="per document; 0 = all")
    ap.add_argument("--qa-concurrency", type=int, default=6)
    ap.add_argument(
        "--namespace-salt", default="", help="deterministic namespaces (pair with --skip-memorize)"
    )
    ap.add_argument("--skip-memorize", action="store_true")
    ap.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="keep this run's namespaces (default: erased on exit; salted ones are always kept)",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
