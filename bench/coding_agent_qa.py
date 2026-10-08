"""Coding-agent end-to-end QA + fix-reuse benchmark for Loom.

The gap this fills: Loom benches conversational QA (`longmemeval.py`,
reader+judge answer-correctness) — but has no end-to-end coding-agent QA and no
fix-reuse metric. Fix-reuse — "given a NEW bug matching a PAST lesson, does
retrieval surface the prior fix at small-k so the agent can apply it?" — is
the metric a coding agent cares about.

What it measures, on a small authored coding-lesson corpus:
  - Retrieval (the coding-agent metric, small-k): R@5, R@10, MRR vs the
    gold lesson keys per question.
  - Fix-reuse: on questions tagged with a `fix_reuse` block (a NEW bug
    matching a PAST lesson), did the prior fix surface in top-k? This is
    the "agent gets to reapply the prior fix" signal.
  - End-to-end QA accuracy (live mode only): a pinned READER synthesizes an
    answer from Loom-retrieved context; a pinned JUDGE grades it.

Pinned-instrument discipline (mirrors `longmemeval.py` exactly):
  reader = gpt-4o-mini, judge = gpt-4o-2024-08-06 (the official snapshot).
  These deliberately do NOT follow LOOM_LLM_MODEL — the server-side model is
  what a bench varies, so an instrument that tracked it would make numbers
  non-comparable. Override with BENCH_READER_MODEL / BENCH_JUDGE_MODEL; point
  at an OpenAI-compatible gateway with OPENAI_BASE_URL.

Questions span a generic coding taxonomy (no external dataset text, no
vendor names): bug-fix-landing, infra-fix, refactor-location, flaky-test,
perf-leak, schema-migration, multi-session-causal (root cause + where fixed
+ post-mortem), temporal ("what shipped on <date>"), team-convention-
preference ("which async runtime do we prefer").

Run:
    # Structure/dry mode — NO server, NO reader/judge, NO API spend.
    # Builds the question set, runs the scorer on a synthetic labeled
    # fixture, asserts R@5/R@10/MRR + fix-reuse compute. The inner-loop gate.
    python bench/coding_agent_qa.py --dry

    # Live retrieval only — ingest lessons, search at small-k, score
    # R@5/R@10/MRR + fix-reuse (no LLM spend).
    python bench/coding_agent_qa.py --ingest --search

    # Full end-to-end — retrieval + reader-synthesized answers + judge.
    # Requires OPENAI_API_KEY.
    python bench/coding_agent_qa.py --ingest --search --answer-judge
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

try:
    from bench._http import apost_json
    from bench.bench_namespaces import register_bench_namespace
except ModuleNotFoundError:  # `python bench/coding_agent_qa.py` puts bench/ on sys.path
    from _http import apost_json  # type: ignore[no-redef]
    from bench_namespaces import register_bench_namespace  # type: ignore[no-redef]

DEFAULT_NAMESPACE = "coding-eval-ns"
DEFAULT_AGENT = "coding-eval-loader"
DEFAULT_ORG = "bench-org"

_DEFAULT_FIXTURE = Path(__file__).parent / "fixtures" / "coding_agent_qa.json"

# Pinned instrument — see module docstring. These do NOT follow LOOM_LLM_MODEL.
_DEFAULT_READER_MODEL = os.environ.get("BENCH_READER_MODEL") or "gpt-4o-mini"
_OFFICIAL_JUDGE_MODEL = "gpt-4o-2024-08-06"
_DEFAULT_JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL") or _OFFICIAL_JUDGE_MODEL
_OPENAI_CHAT_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip(
    "/"
) + "/chat/completions"

# Small-k is the coding-agent retrieval regime: an agent reads a handful of
# memories before acting, not 200. Score recall at 5 and 10.
_RECALL_KS = (5, 10)


# --------------------------------------------------------------------------
# Pure scoring core. CH-free, LLM-free, unit-tested by
# bench/test_coding_agent_qa.py. Everything below `_run_search` depends on a
# live server; everything here is deterministic given retrieved_keys.
# --------------------------------------------------------------------------


def recall_at_k(gold_keys: list[str], retrieved_keys: list[str], k: int) -> float:
    """Fraction of gold keys present in the top-k retrieved keys."""
    gold = set(gold_keys)
    if not gold:
        return 0.0
    topk = set(retrieved_keys[:k])
    return len(topk & gold) / len(gold)


def mrr(gold_keys: list[str], retrieved_keys: list[str]) -> float:
    """Reciprocal rank of the FIRST gold key in the retrieved list."""
    gold = set(gold_keys)
    for rank, key in enumerate(retrieved_keys, start=1):
        if key in gold:
            return 1.0 / rank
    return 0.0


def fix_reuse_hit(prior_fix_key: str, retrieved_keys: list[str], k: int) -> bool:
    """Did the prior fix surface in top-k so the agent can reapply it?

    This is the fix-reuse metric: a NEW bug matching a PAST lesson is only
    actionable if the prior fix is retrievable at the small-k the agent
    actually reads.
    """
    return prior_fix_key in set(retrieved_keys[:k])


@dataclass
class QAResult:
    question: str
    question_type: str
    gold_keys: list[str]
    retrieved_keys: list[str]
    r_at_5: float
    r_at_10: float
    mrr: float
    # Fix-reuse: None when the question carries no fix_reuse block; else
    # whether the prior fix surfaced in top-5 (the actionable-k).
    fix_reuse_prior_key: str = ""
    fix_reuse_hit_at_5: bool | None = None
    # Live-only fields (None in dry mode).
    hypothesis: str = ""
    judge_label: str | None = None
    judge_rationale: str = ""


def score_question(
    gold_keys: list[str],
    retrieved_keys: list[str],
    *,
    question: str = "",
    question_type: str = "",
    fix_reuse_key: str = "",
) -> QAResult:
    """Score one question's retrieval. Pure; no IO."""
    return QAResult(
        question=question,
        question_type=question_type,
        gold_keys=list(gold_keys),
        retrieved_keys=list(retrieved_keys),
        r_at_5=recall_at_k(gold_keys, retrieved_keys, 5),
        r_at_10=recall_at_k(gold_keys, retrieved_keys, 10),
        mrr=mrr(gold_keys, retrieved_keys),
        fix_reuse_prior_key=fix_reuse_key,
        fix_reuse_hit_at_5=(
            fix_reuse_hit(fix_reuse_key, retrieved_keys, 5) if fix_reuse_key else None
        ),
    )


@dataclass
class Summary:
    n: int = 0
    r_at_5: float = 0.0
    r_at_10: float = 0.0
    mrr: float = 0.0
    qa_accuracy: float | None = None
    n_judged: int = 0
    # Fix-reuse rollup over only the questions that carry a fix_reuse block.
    n_fix_reuse: int = 0
    fix_reuse_rate_at_5: float | None = None
    per_type: dict[str, dict[str, float]] = field(default_factory=dict)


def summarize(results: list[QAResult]) -> Summary:
    """Aggregate per-question results into headline + per-type + fix-reuse."""
    if not results:
        return Summary()
    s = Summary(
        n=len(results),
        r_at_5=statistics.mean(r.r_at_5 for r in results),
        r_at_10=statistics.mean(r.r_at_10 for r in results),
        mrr=statistics.mean(r.mrr for r in results),
    )

    fr = [r for r in results if r.fix_reuse_hit_at_5 is not None]
    s.n_fix_reuse = len(fr)
    if fr:
        s.fix_reuse_rate_at_5 = statistics.mean(1.0 if r.fix_reuse_hit_at_5 else 0.0 for r in fr)

    judged = [r for r in results if r.judge_label is not None]
    s.n_judged = len(judged)
    if judged:
        s.qa_accuracy = statistics.mean(1.0 if r.judge_label == "CORRECT" else 0.0 for r in judged)

    by_type: dict[str, list[QAResult]] = {}
    for r in results:
        by_type.setdefault(r.question_type, []).append(r)
    for qtype, rs in by_type.items():
        s.per_type[qtype] = {
            "n": float(len(rs)),
            "r_at_5": statistics.mean(r.r_at_5 for r in rs),
            "r_at_10": statistics.mean(r.r_at_10 for r in rs),
            "mrr": statistics.mean(r.mrr for r in rs),
        }
    return s


def load_corpus(path: Path) -> tuple[list[dict], list[dict]]:
    """Load (lessons, questions) from the fixture. Validates that every
    question's gold_keys (and fix_reuse prior_fix_key) reference a real
    lesson key — a dangling gold key is a corpus bug, not a recall miss."""
    data = json.loads(path.read_text())
    lessons = data["lessons"]
    questions = data["questions"]
    keys = {ln["memory_key"] for ln in lessons}
    for q in questions:
        for gk in q.get("gold_keys", []):
            if gk not in keys:
                raise ValueError(f"question gold_key {gk!r} has no matching lesson")
        fr = q.get("fix_reuse") or {}
        pk = fr.get("prior_fix_key")
        if pk and pk not in keys:
            raise ValueError(f"fix_reuse prior_fix_key {pk!r} has no matching lesson")
    return lessons, questions


def build_synthetic_retrieval(questions: list[dict]) -> dict[str, list[str]]:
    """Build a deterministic, labeled retrieval result per question for dry
    mode — NO server call. Models a realistic-but-imperfect retriever:
      - gold keys land at the top (so R@5 / fix-reuse register), but
      - a couple of distractor keys are interleaved so MRR < 1.0 on
        multi-gold questions and the scorer is exercised on a non-trivial
        ranking (not a degenerate all-gold list).
    Returns {question_text: retrieved_keys}.
    """
    out: dict[str, list[str]] = {}
    distractors = [f"distractor_{i}" for i in range(10)]
    for qi, q in enumerate(questions):
        gold = list(q.get("gold_keys", []))
        # Interleave: first gold at rank 1, then one distractor, then the
        # rest of the gold, then pad with distractors out to 12 hits.
        ranked: list[str] = []
        if gold:
            ranked.append(gold[0])
        ranked.append(distractors[qi % len(distractors)])
        ranked.extend(gold[1:])
        for d in distractors:
            if len(ranked) >= 12:
                break
            if d not in ranked:
                ranked.append(d)
        out[q["question"]] = ranked
    return out


# --------------------------------------------------------------------------
# Live-server paths (ingest + search + reader + judge). Skipped in --dry.
# --------------------------------------------------------------------------


def _identity(namespace: str) -> dict:
    return {
        "org": DEFAULT_ORG,
        "namespace": namespace,
        "agent": DEFAULT_AGENT,
        "user_id": "-",
    }


async def _post(client: httpx.AsyncClient, base_url: str, path: str, body: dict) -> dict:
    return await apost_json(client, base_url + path, body, timeout_s=180.0)


async def _ingest(
    client: httpx.AsyncClient,
    base_url: str,
    namespace: str,
    lessons: list[dict],
    batch_size: int = 100,
) -> int:
    """Bulk-write lessons via memory.bulk_set (caller-decided saves, no LLM
    extraction). Returns the number of
    batches that failed."""
    identity = _identity(namespace)
    n = len(lessons)
    written = failed = 0
    for i in range(0, n, batch_size):
        batch = lessons[i : i + batch_size]
        entries = [
            {
                "memory_key": ln["memory_key"],
                "memory_type": ln["memory_type"],
                "value": ln["value"],
                "tags": ln.get("tags") or [],
            }
            for ln in batch
        ]
        body = {**identity, "entries": entries}
        try:
            await _post(client, base_url, "/v1/memory.bulk_set", body)
            written += len(batch)
            print(f"  ingested {written}/{n}", flush=True)
        except httpx.HTTPStatusError as e:
            failed += 1
            print(
                f"  ! batch {i}-{i + batch_size} failed: {e.response.text[:200]}",
                file=sys.stderr,
                flush=True,
            )
    return failed


async def _search_keys(
    client: httpx.AsyncClient,
    base_url: str,
    namespace: str,
    query: str,
    search_mode: str,
    alpha: float,
    top_k: int,
) -> tuple[list[str], list[dict]] | None:
    """Return (retrieved_keys, hits) for one query at small top_k; None when
    the search failed (the question is then excluded, not scored as a miss)."""
    body = {
        **_identity(namespace),
        "query": query,
        "top_k": top_k,
        "search_mode": search_mode,
        "alpha": alpha,
    }
    try:
        resp = await _post(client, base_url, "/v1/memory.search", body)
    except httpx.HTTPError as e:
        print(f"  ! search failed for {query[:50]!r}: {e}", file=sys.stderr, flush=True)
        return None
    hits = resp.get("results", []) or []
    keys = [h.get("memory_key", "") for h in hits if h.get("memory_key")]
    return keys, hits


_READER_TEMPLATE = (
    "You are a coding agent with access to memories (lessons, bug fixes, and "
    "team conventions) from past work on this codebase. Answer the question "
    "using ONLY these memories. Be direct: name the file, the fix, and the "
    "date when the memories contain them. If the memories do not contain the "
    "answer, say the information is not available.\n\n"
    "Memories:\n{history}\n\nQuestion: {question}\nAnswer:"
)


def _build_reader_prompt(question: str, hits: list[dict]) -> str:
    blocks: list[str] = []
    for i, h in enumerate(hits):
        content = (h.get("content_excerpt") or "").strip()
        blocks.append(f"### Memory {i + 1} (key={h.get('memory_key', '')}):\n{content}\n")
    history = "\n".join(blocks) or "(no memories retrieved)"
    return _READER_TEMPLATE.format(history=history, question=question)


async def _llm_synthesize(
    client: httpx.AsyncClient,
    question: str,
    hits: list[dict],
    model: str,
    api_key: str,
) -> str:
    body = {
        "model": model,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": _build_reader_prompt(question, hits)}],
    }
    try:
        r = await client.post(
            _OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        return f"(synthesis error: {e})"


def _judge_prompt(question: str, gold_text: str, response: str) -> str:
    """Coding-QA judge prompt. Same shape as the LongMemEval official judge:
    yes if the response contains the correct answer; temperature 0,
    max_tokens 10, label = 'yes' in reply."""
    return (
        "I will give you a question about a codebase, a correct answer, and a "
        "response from a coding agent. Please answer yes if the response "
        "contains the correct answer (the right file, fix, date, or "
        "convention asked for). Otherwise answer no. If the response is "
        "equivalent to the correct answer or contains all the information "
        "required by it, answer yes. If it only contains a subset of the "
        f"required information, answer no.\n\nQuestion: {question}\n\nCorrect "
        f"Answer: {gold_text}\n\nResponse: {response}\n\nIs the response "
        "correct? Answer yes or no only."
    )


async def _llm_judge(
    client: httpx.AsyncClient,
    question: str,
    gold_text: str,
    response: str,
    model: str,
    api_key: str,
) -> tuple[str | None, str]:
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": _judge_prompt(question, gold_text, response)}],
    }
    try:
        r = await client.post(
            _OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
        r.raise_for_status()
        reply = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return ("CORRECT" if "yes" in reply.lower() else "WRONG"), reply
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        return None, f"judge error: {e}"


def _gold_text_for(question: dict, lessons_by_key: dict[str, dict]) -> str:
    """Build the judge's reference answer from the gold lessons' text/fix."""
    parts: list[str] = []
    for gk in question.get("gold_keys", []):
        ln = lessons_by_key.get(gk)
        if not ln:
            continue
        v = ln.get("value") or {}
        txt = v.get("text") or ""
        fix = v.get("fix") or ""
        files = ", ".join(v.get("files") or [])
        seg = txt
        if fix:
            seg += f" Fix: {fix}"
        if files:
            seg += f" Files: {files}"
        parts.append(seg.strip())
    return " | ".join(p for p in parts if p)


async def _run_live(
    base_url: str,
    namespace: str,
    lessons: list[dict],
    questions: list[dict],
    *,
    do_ingest: bool,
    do_search: bool,
    answer_judge: bool,
    search_mode: str,
    alpha: float,
    top_k: int,
    reader_model: str,
    judge_model: str | None,
    judge_api_key: str,
    token: str = "",
) -> tuple[list[QAResult], int]:
    """(scored results, failures): a failure is a failed ingest batch or a
    failed search; an unhealthy server raises."""
    lessons_by_key = {ln["memory_key"]: ln for ln in lessons}
    results: list[QAResult] = []
    failures = 0
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    # `loom` carries the bench bearer; `client` talks to the reader/judge API
    async with httpx.AsyncClient() as client, httpx.AsyncClient(headers=auth) as loom:
        h = await loom.get(base_url + "/v1/health", timeout=5.0)
        if h.status_code != 200:
            raise RuntimeError(f"server unhealthy: {h.status_code}")

        if do_ingest:
            # a reused corpus (ingest once, search many runs): bench traffic, kept
            await register_bench_namespace(
                lambda path, body: _post(loom, base_url, path, body),
                org=DEFAULT_ORG,
                namespace=namespace,
                description="coding_agent_qa lessons corpus",
                keep=True,
            )
            print(f"--- ingesting {len(lessons)} lessons into {namespace}")
            failures += await _ingest(loom, base_url, namespace, lessons)

        if not do_search:
            return results, failures

        for q in questions:
            got = await _search_keys(
                loom, base_url, namespace, q["question"], search_mode, alpha, top_k
            )
            if got is None:
                failures += 1
                continue
            keys, hits = got
            fr = q.get("fix_reuse") or {}
            res = score_question(
                q.get("gold_keys", []),
                keys,
                question=q["question"],
                question_type=q.get("question_type", ""),
                fix_reuse_key=fr.get("prior_fix_key", ""),
            )
            if answer_judge and judge_model and judge_api_key:
                res.hypothesis = await _llm_synthesize(
                    client, q["question"], hits, reader_model, judge_api_key
                )
                gold_text = _gold_text_for(q, lessons_by_key)
                label, rationale = await _llm_judge(
                    client, q["question"], gold_text, res.hypothesis, judge_model, judge_api_key
                )
                res.judge_label = label
                res.judge_rationale = rationale
            results.append(res)
    return results, failures


def _print_summary(s: Summary, *, label: str, reader: str = "", judge: str | None = None) -> None:
    print(f"\n=== {label} (n={s.n}) ===")
    print(f"  R@5:  {s.r_at_5:.3f}")
    print(f"  R@10: {s.r_at_10:.3f}")
    print(f"  MRR:  {s.mrr:.3f}")
    if s.fix_reuse_rate_at_5 is not None:
        print(f"  fix-reuse@5: {s.fix_reuse_rate_at_5:.3f}  (n={s.n_fix_reuse})")
    if s.qa_accuracy is not None:
        print(
            f"  QA accuracy: {s.qa_accuracy:.3f}  (n_judged={s.n_judged}; "
            f"reader={reader}, judge={judge})"
        )
    print(f"\n  {'type':<28} {'n':>3} {'R@5':>6} {'R@10':>6} {'MRR':>6}")
    for qtype, pt in sorted(s.per_type.items()):
        print(
            f"  {qtype:<28} {int(pt['n']):>3} "
            f"{pt['r_at_5']:>6.3f} {pt['r_at_10']:>6.3f} {pt['mrr']:>6.3f}"
        )


def _run_dry(questions: list[dict]) -> int:
    """Structure mode — NO server, NO reader/judge, NO API spend.

    Asserts the harness (a) builds the question set, (b) produces small-k
    retrieval (here synthetic + labeled), and (c) computes R@5/R@10/MRR +
    fix-reuse on it. This is the inner-loop gate; the full LLM run is deferred.
    """
    print(f"--- DRY/STRUCTURE mode: {len(questions)} questions, no server, no LLM")
    assert questions, "question set is empty"

    synth = build_synthetic_retrieval(questions)
    results: list[QAResult] = []
    for q in questions:
        retrieved = synth[q["question"]]
        assert retrieved, f"synthetic retrieval empty for {q['question']!r}"
        fr = q.get("fix_reuse") or {}
        results.append(
            score_question(
                q.get("gold_keys", []),
                retrieved,
                question=q["question"],
                question_type=q.get("question_type", ""),
                fix_reuse_key=fr.get("prior_fix_key", ""),
            )
        )

    s = summarize(results)
    # Sanity: the synthetic retriever places every gold key in the top
    # ranks, so R@5 must be perfect and MRR must be 1.0 (first gold at
    # rank 1). Fix-reuse questions must all hit@5. These assertions are the
    # gate — if the scorer breaks, --dry fails loudly without any spend.
    assert s.r_at_5 == 1.0, f"expected R@5=1.0 on synthetic gold-first fixture, got {s.r_at_5}"
    assert s.r_at_10 == 1.0, f"expected R@10=1.0, got {s.r_at_10}"
    assert s.mrr == 1.0, f"expected MRR=1.0 (gold at rank 1), got {s.mrr}"
    assert s.n_fix_reuse > 0, "fixture has no fix_reuse questions to exercise"
    assert s.fix_reuse_rate_at_5 == 1.0, (
        f"expected fix-reuse@5=1.0 on synthetic fixture, got {s.fix_reuse_rate_at_5}"
    )
    assert s.qa_accuracy is None, "dry mode must not invoke the judge"

    _print_summary(s, label="DRY (synthetic labeled fixture)")
    print(
        "\nOK: question set built, small-k search shape exercised, "
        "R@5/R@10/MRR + fix-reuse computed. No API spend."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(_DEFAULT_FIXTURE))
    parser.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", ""))
    parser.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--dry",
        action="store_true",
        help="Structure mode: build the question set + score a synthetic "
        "labeled fixture. No server, no reader/judge, no API spend.",
    )
    parser.add_argument("--ingest", action="store_true", help="Bulk-write lessons (live).")
    parser.add_argument("--search", action="store_true", help="Run queries + score (live).")
    parser.add_argument(
        "--answer-judge",
        action="store_true",
        help="Synthesize answers (pinned reader) + grade (pinned judge). Requires OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--search-mode",
        default="rrf",
        help="memory.search mode (rrf, semantic, hybrid, bm25_hybrid, ...).",
    )
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Small-k retrieval depth — the coding-agent regime (default 10).",
    )
    parser.add_argument(
        "--reader-model",
        default="",
        help=f"Reader (default pinned {_DEFAULT_READER_MODEL}; BENCH_READER_MODEL).",
    )
    parser.add_argument(
        "--judge-model",
        default="",
        help=f"Judge (default pinned official {_OFFICIAL_JUDGE_MODEL}; BENCH_JUDGE_MODEL).",
    )
    args = parser.parse_args()

    fixture = Path(args.fixture)
    if not fixture.exists():
        print(f"ERROR: fixture not found at {fixture}", file=sys.stderr)
        return 2
    lessons, questions = load_corpus(fixture)
    print(f"loaded {len(lessons)} lessons, {len(questions)} questions from {fixture.name}")

    if args.dry:
        return _run_dry(questions)

    if not (args.ingest or args.search):
        print(
            "Nothing to do: pass --dry for the offline gate, or --ingest/--search for a live run.",
            file=sys.stderr,
        )
        return 2

    if not args.base_url:
        print("--base-url (or LOOM_BASE_URL) is required for a live run", file=sys.stderr)
        return 2

    reader_model = args.reader_model or _DEFAULT_READER_MODEL
    judge_model: str | None = None
    judge_api_key = ""
    if args.answer_judge:
        judge_api_key = os.environ.get("OPENAI_API_KEY", "")
        if not judge_api_key:
            print("--answer-judge requires OPENAI_API_KEY", file=sys.stderr)
            return 2
        judge_model = args.judge_model or _DEFAULT_JUDGE_MODEL
        if judge_model == "gpt-4o":
            judge_model = _OFFICIAL_JUDGE_MODEL
        if judge_model != _OFFICIAL_JUDGE_MODEL:
            print(
                f"WARNING: judge_model={judge_model} is not the official "
                f"{_OFFICIAL_JUDGE_MODEL}; QA accuracy will not be comparable.",
                file=sys.stderr,
            )
        if reader_model != "gpt-4o-mini":
            print(
                f"NOTE: reader_model={reader_model} differs from the pinned "
                "baseline reader (gpt-4o-mini); QA accuracy is a reader "
                "property, not just a memory property.",
                file=sys.stderr,
            )

    try:
        results, failures = asyncio.run(
            _run_live(
                args.base_url,
                args.namespace,
                lessons,
                questions,
                do_ingest=args.ingest,
                do_search=args.search,
                answer_judge=args.answer_judge,
                search_mode=args.search_mode,
                alpha=args.alpha,
                top_k=args.top_k,
                reader_model=reader_model,
                judge_model=judge_model,
                judge_api_key=judge_api_key,
                token=args.token,
            )
        )
    except (RuntimeError, httpx.HTTPError) as e:
        print(f"live run failed: {e}", file=sys.stderr)
        return 1
    if failures:
        print(f"{failures} ingest batch(es) or search(es) failed", file=sys.stderr)
    if not results:
        return 1 if failures else 0  # an ingest-only run

    s = summarize(results)
    _print_summary(
        s,
        label=f"LIVE [{args.search_mode}, top_k={args.top_k}]",
        reader=reader_model,
        judge=judge_model,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
