"""LongMemEval recall@K and answer-judge accuracy against Loom.

LongMemEval-S (`longmemeval_s_cleaned.json`, the default dataset) is 500
question/answer pairs where each question's "haystack" is ~40-50
conversation sessions, mostly distractors — real retrieval pressure. The
evidence-only oracle split (`longmemeval_oracle.json`, ~2 sessions/item,
selectable via --dataset) has near-zero retrieval pressure: its numbers
are NOT comparable to -S figures in either direction. We:

  1. For each item, create a fresh namespace.
  2. Extract memories from every session via memory.set_from_messages,
     forwarding the session's date as observation_date so the extractor
     can resolve relative time references ("last week" → ISO).
  3. Run memory.search with the question.
  4. Score one of two ways:
     • Default (substring recall@K): does the gold answer text appear as
       a substring of any top-K excerpt? Strict IR baseline.
     • --answer-judge: a fixed READER (gpt-4o-mini) synthesizes an answer
       from the top-K, then the official LongMemEval JUDGE
       (gpt-4o-2024-08-06) grades it CORRECT/WRONG. Reader and judge are
       the measuring instrument: they're pinned, and deliberately do NOT
       follow LOOM_LLM_MODEL — that's the server-side variable bench runs
       exist to vary. Override with BENCH_READER_MODEL / BENCH_JUDGE_MODEL;
       a non-official judge prints a comparability warning.

Cost (dominated by server-side extraction — one LLM call per session):
  - Oracle split: ~$7.50 of gpt-4o-mini on 500 items (~2 sessions/item).
  - Default -S split: ~25x that (~47 sessions/item). Use --limit for
    smoke runs.
  - With --answer-judge:    +$1–$2 of reader + judge LLM calls.

Run:
    python bench/longmemeval.py --limit 50 --out bench/longmemeval.csv
    python bench/longmemeval.py --limit 50 --answer-judge \\
        --out bench/longmemeval_judge.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx

try:
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

# The relevance-gate prompt + parser are the server's own so
# the paired arm below judges with byte-identical prompts to memory.ask.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench._gate import (
    add_gate_arguments,
    agreement,
    flip_rate,
    gate_from_args,
    identical_share,
    majority_label,
    measurement_fields,
)
from bench.judges import achat, is_claude
from bench.relevance_gate import (
    GATE_SYSTEM,
    apply_verdict,
    build_gate_user,
    parse_relevant,
)
from server.memory.domain.relevance_gate import similarity_floor_cut

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_HAYSTACK_DATE_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})")


def _norm(s: str) -> str:
    """Light normalization for substring matching — lowercase + collapse
    non-alphanumerics. Keeps multi-word answer phrases findable even if
    Loom's content_excerpt punctuates differently."""
    return _NORMALIZE_RE.sub(" ", s.lower()).strip()


def _to_iso_date(longmemeval_date: str) -> str:
    """LongMemEval dates look like '2023/04/10 (Mon) 17:50' — return the
    ISO YYYY-MM-DD prefix so the extractor's observation_date arg can
    consume it directly."""
    m = _HAYSTACK_DATE_RE.match(longmemeval_date or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


# ============================================================
# Reader + judge — VERBATIM the official LongMemEval setup
# ============================================================
# To keep the QA number comparable to published LongMemEval results, Loom
# plugs in at indexing+retrieval ONLY; the downstream READER and JUDGE are
# the official harness components, held fixed. We deliberately use NO custom
# reader and NO custom judge — either would be bench tuning that inflates a
# non-comparable number. Retrieval recall@k is reported separately as Loom's
# own (objective) optimization metric.
# Reader and judge identities are held FIXED, independent of LOOM_LLM_MODEL.
# LOOM_LLM_MODEL configures the server-side extraction model — the thing a
# bench run varies — so an instrument that follows it grades each candidate
# with a different reader+judge and the numbers stop being comparable.
_DEFAULT_READER_MODEL = os.environ.get("BENCH_READER_MODEL") or "gpt-4o-mini"
# The exact snapshot the official evaluate_qa.py model_zoo pins for "gpt-4o".
_OFFICIAL_JUDGE_MODEL = "gpt-4o-2024-08-06"
_DEFAULT_JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL") or _OFFICIAL_JUDGE_MODEL
_JUDGE_ATTEMPTS = 4
# Reader/judge calls accept any OpenAI-compatible endpoint (gateway, proxy,
# vLLM) — parity with bench/official run_generation.py's --openai_base_url.
_OPENAI_CHAT_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip(
    "/"
) + "/chat/completions"

# READER: the official answer prompt from src/generation/run_generation.py —
# the "facts extracted from history chats" variant (Loom returns extracted
# facts), step-by-step reasoning (cot=True, the harness's reasoning path).
# Single user message, NO system prompt, and none of our own
# abstention/brevity/recency rules. The model's FULL completion is the
# hypothesis the judge grades (no answer-parsing of our own).
_OFFICIAL_ANSWER_TEMPLATE = (
    "I will give you several facts extracted from history chats between you "
    "and a user. Please answer the question based on the relevant facts. "
    "Answer the question step by step: first extract all the relevant "
    "information, and then reason over the information to get the answer."
    "\n\n\nHistory Chats:\n\n{history}\n\nCurrent Date: {date}\nQuestion: "
    "{question}\nAnswer (step by step):"
)

# Optional verbose-reasoning reader (--reader tuned). Adopts general
# answer-synthesis heuristics — compute-don't-abstain, scan-all-positions
# counting, exact-entity discipline, recency-wins, inclusive time windows,
# preference personalization — but NO answer-specific carve-outs. This is a
# DISCLOSED bench-tuning posture, kept only as an ablation to show the reader's
# headroom; the DEFAULT and reported reader is the official untuned one above.
# The JUDGE stays the official per-type evaluate_qa.py regardless.
_TUNED_ANSWER_TEMPLATE = (
    "You are a personal assistant with access to memories (facts extracted "
    "from past conversations with the user). Answer the question using ONLY "
    "these memories. Be direct and concise.\n\n"
    "Today's date is {date}. Compute every relative time expression ('last "
    "month', 'two weeks ago', 'how many days ago') relative to this date.\n\n"
    "Reason step by step inside <thinking>...</thinking>; only text OUTSIDE the "
    "tags is shown as the answer.\n"
    "- List every memory relevant to the question.\n"
    "- COUNTING: build a numbered list with each item's date; scan ALL memories "
    "first-to-last, then do a SECOND pass (relevant items can sit anywhere, not "
    "just the top); apply the question's exact qualifier/verb strictly; count "
    "multiple items in one memory separately; dedupe by date/description; "
    "exclude plans ('plans to', 'will') unless a later memory confirms the "
    "action.\n"
    "- COMPUTATION: if the memories contain the numbers needed (ages, prices, "
    "dates to diff, durations), DO the arithmetic — never abstain just because "
    "the facts are scattered across different memories. Show the computation.\n"
    "- TEMPORAL: 'how many days ago did X when Y happened' = the interval "
    "between X and Y, not between X and today. Use inclusive date windows.\n"
    "- CONTEXT: only use a memory's value if its context matches the question "
    "(a 'while travelling' routine is not a regular weekday routine); prefer "
    "the more specific matching memory.\n"
    "- RECENCY: for conflicting values of the same fact the most recent memory "
    "wins; a later count/status replaces earlier ones (don't sum). Memories "
    "about different people/contexts don't conflict.\n"
    "- EXACT ENTITY: answer about the EXACT entity/variant/role asked. If the "
    "question names a specific variant or title and the memories only mention a "
    "different one (electric vs acoustic guitar; 'Sales Manager' vs 'Senior "
    "Sales Engineer'), say you don't have that information — do not conflate.\n"
    "- COMPARISON: for savings/comparison questions both sides must come from "
    "user-stated facts; if only one side is present, say it's not enough.\n"
    "- PREFERENCES/SUGGESTIONS: lead with the user's known preferences; apply "
    "them to new contexts; respect stated dislikes; reference tools/apps the "
    "user already owns.\n\n"
    "Answer rules: use ONLY the memories (never invent numbers/names/dates); be "
    "COMPLETE (keep every qualifier — colours, counts, units, full names); "
    "abstain ('The information provided is not enough') ONLY when the topic is "
    "genuinely unmentioned, the specific entity asked about doesn't exist, or a "
    "comparison's required side is missing.\n\n"
    "Memories (facts extracted from past chats):\n{history}\n\n"
    "Today's date: {date}\nQuestion: {question}\n"
    "Answer (reason in <thinking>, then give the final answer):"
)


# Judge methodology: VERBATIM the official LongMemEval judge
# (src/evaluation/evaluate_qa.py::get_anscheck_prompt), the same
# official per-question-type grading prompts (the published methodology). We
# do NOT use a custom/generous judge — that would inflate a non-comparable
# number.
# gpt-4o-2024-08-06, temperature 0, max_tokens 10, label = 'yes' in reply.
def _official_anscheck_prompt(
    task: str, question: str, answer: str, response: str, abstention: bool = False
) -> str:
    if abstention:
        return (
            "I will give you an unanswerable question, an explanation, and a "
            "response from a model. Please answer yes if the model correctly "
            "identifies the question as unanswerable. The model could say that "
            "the information is incomplete, or some other information is given "
            f"but the asked information is not.\n\nQuestion: {question}\n\nExplanation: "
            f"{answer}\n\nModel Response: {response}\n\nDoes the model correctly identify the "
            "question as unanswerable? Answer yes or no only."
        )
    base = (
        "I will give you a question, a correct answer, and a response from a "
        "model. Please answer yes if the response contains the correct answer. "
        "Otherwise, answer no. If the response is equivalent to the correct "
        "answer or contains all the intermediate steps to get the correct "
        "answer, you should also answer yes. If the response only contains a "
        "subset of the information required by the answer, answer no. {}"
        "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the "
        "model response correct? Answer yes or no only."
    )
    if task in ("single-session-user", "single-session-assistant", "multi-session"):
        return base.format("", question, answer, response)
    if task == "temporal-reasoning":
        lenient = (
            "In addition, do not penalize off-by-one errors for the number of "
            "days. If the question asks for the number of days/weeks/months, "
            "etc., and the model makes off-by-one errors (e.g., predicting 19 "
            "days when the answer is 18), the model's response is still correct. "
        )
        return base.format(lenient, question, answer, response)
    if task == "knowledge-update":
        return (
            "I will give you a question, a correct answer, and a response from a "
            "model. Please answer yes if the response contains the correct "
            "answer. Otherwise, answer no. If the response contains some "
            "previous information along with an updated answer, the response "
            "should be considered as correct as long as the updated answer is "
            f"the required answer.\n\nQuestion: {question}\n\nCorrect Answer: {answer}\n\nModel "
            f"Response: {response}\n\nIs the model response correct? Answer yes or no only."
        )
    if task == "single-session-preference":
        return (
            "I will give you a question, a rubric for desired personalized "
            "response, and a response from a model. Please answer yes if the "
            "response satisfies the desired response. Otherwise, answer no. The "
            "model does not need to reflect all the points in the rubric. The "
            "response is correct as long as it recalls and utilizes the user's "
            f"personal information correctly.\n\nQuestion: {question}\n\nRubric: {answer}\n\n"
            f"Model Response: {response}\n\nIs the model response correct? Answer yes or "
            "no only."
        )
    raise ValueError(f"unknown question_type for judge: {task}")


def _build_answer_user_prompt(
    question: str,
    hits: list[dict],
    question_date: str = "",
    reader: str = "official",
) -> str:
    """Build the reader prompt. Each retrieved memory is rendered as a dated
    "Memory" block (mirroring run_generation.py's per-session block), sorted by
    date as the harness does, then dropped into the chosen answer template.

    reader='official' (default): the standard run_generation.py prompt verbatim
    — no editorial rules of ours; only WHICH facts appear is Loom's retrieval.
    reader='tuned': a verbose-reasoning posture (disclosed bench tuning).
    """

    def _date_of(h: dict) -> str:
        d = (h.get("valid_at") or h.get("temporal_anchor") or "").strip()
        if d.startswith("1970-01-01"):  # toDateTime(0) sentinel = undated
            return ""
        return d[:10]

    ordered = sorted(hits, key=lambda h: _date_of(h) or "9999")  # undated last
    blocks: list[str] = []
    for i, h in enumerate(ordered):
        content = (h.get("content_excerpt") or "").strip()
        date = _date_of(h) or "unknown"
        blocks.append(f"### Memory {i + 1}:\nDate: {date}\nContent:\n{content}\n")
    history_string = "\n".join(blocks) or "(no facts retrieved)"
    template = _TUNED_ANSWER_TEMPLATE if reader == "tuned" else _OFFICIAL_ANSWER_TEMPLATE
    return template.format(
        history=history_string, date=question_date or "unknown", question=question
    )


@dataclass
class ItemResult:
    question_id: str
    question_type: str
    question: str
    gold_answer: str
    top_k_excerpts: list[str]
    recalled: bool
    # Fact-level diagnostic: did the gold answer text actually surface in a
    # retrieved excerpt? (vs `recalled` = evidence-SESSION match). Meaningful
    # for stated-fact categories; ~0 for computed-answer categories by design.
    answer_in_context: bool = False
    # Only populated when --answer-judge is set. None otherwise.
    judge_label: str | None = None
    judge_rationale: str = ""
    # The CoN-reader's final answer, graded by the official judge. Emitted to
    # the hypotheses JSONL so the official evaluate_qa.py can be run verbatim.
    hypothesis: str = ""
    # knowledge-update only, with --stale-check: "1" when a WRONG answer is the
    # SUPERSEDED value from the earlier answer session (a "hallucination of the past"),
    # "0" when wrong for another reason or correct, "" otherwise.
    stale_answer: str = ""
    # Graph diagnostics — populated from MemorySearchOutput. Tells us
    # whether the graph leg fired for this question (graph_seeds > 0) and
    # how many neighbor entities the 1-hop expansion added.
    graph_seeds: int = 0
    graph_neighbors_added: int = 0
    # Graph v2 — relation typing. Triples LLM-extracted from the query,
    # and memory_keys whose stored triples matched.
    triples_extracted_from_query: int = 0
    triples_matched: int = 0
    # Run-config + per-item diagnostics for matrix-runner attribution.
    # Filled by _run_one_item from the caller's CLI args + the
    # search/judge round-trips so the CSV row can be sliced by config.
    search_mode: str = ""
    rerank: str = ""
    pool_unmatched: int = 0
    search_latency_ms: int = 0
    # Efficiency: what it cost to BUILD this item's memory
    # (extraction calls: tokens in/out, calls, memories written, wall time)
    # and to ANSWER (search + reader + judge tokens, wall time). The columns
    # the memory-efficiency literature reports next to accuracy.
    build_tokens_in: int = 0
    build_tokens_out: int = 0
    # Subset of build_tokens_in the provider served from its prompt cache
    # (server-reported `cached_tokens_in`); the shared static prompt.
    build_tokens_cached: int = 0
    build_calls: int = 0
    build_segments: int = 0  # extraction calls the server actually made (topic segments)
    consolidated_clusters: int = 0  # clusters merged by --consolidate before QA
    memories_written: int = 0
    build_seconds: float = 0.0
    qa_tokens_in: int = 0
    qa_tokens_out: int = 0
    qa_calls: int = 0
    qa_seconds: float = 0.0
    # --top-k-sweep: the k this row was answered at (0 = single-k run). Build
    # columns are filled on the FIRST k's row only so efficiency sums stay
    # honest when one ingest serves several k.
    sweep_k: int = 0
    # --answerer: "reader" (pinned external reader over memory.search hits)
    # or "loom-ask" (server-side memory.ask synthesis).
    answerer: str = "reader"
    # memory.ask diagnostics (loom-ask only): which reading depth answered,
    # whether the deep-evidence pass fired, which cascade tier, wall time.
    ask_depth: str = ""
    ask_deep_fired: bool = False
    ask_route_tier: str = ""
    # --answerer loom-ask: Jaccard of the sessions memory.ask CITED
    # (`cited_sessions`) against the item's gold answer sessions — grounding
    # per ANSWER, not per retrieval. -1 when the answerer is the reader or the server sent none.
    cited_session_jaccard: float = -1.0
    ask_latency_ms: int = 0
    # Server-estimated tokens of the context the reader saw (memory.ask
    # `context_tokens_est`); 0 when the answerer is not loom-ask.
    context_tokens_est: int = 0
    # Paired relevance-gate arm (--relevance-gate): the SAME
    # retrieved hits filtered by the server's gate prompt before the reader.
    # Same ingest, same retrieval, same judge — only the reader's input moves.
    gate_mode: str = ""
    gate_kept: int = 0
    gate_dropped: int = 0
    gate_answer_in_context: bool = False
    gate_judge_label: str | None = None
    gate_hypothesis: str = ""
    # --ask-arms: the label of the memory.ask configuration this row was
    # answered with ("" for a single-configuration run). Arms share one ingest
    # and one set of search hits per k, so per-item deltas between arms are
    # paired — the shape bench/bootstrap.paired_bootstrap gates on.
    arm: str = ""
    # Robustness variant applied before ingest (bench/lme_variants.py):
    # "position=early", "scale=2", ...; "" for the unmodified item.
    variant: str = ""
    # Lexical scores comparable with published tables (bench/answer_scoring.py):
    # token F1 and BLEU-1 of the SIMPLIFIED hypothesis against the gold answer,
    # with the published tokenisation. `simplified_by` = "model:<name>" or
    # "heuristic" (no second model). Zero and "" when --lexical-scores is off.
    f1: float = 0.0
    bleu1: float = 0.0
    # ROUGE-L F1 / recall of the same simplified hypothesis.
    rouge_l_f1: float = 0.0
    rouge_l_recall: float = 0.0
    # --repeats N: every judged label this item received, in
    # run order; the share agreeing with the majority written to
    # `judge_label`; and the share of runs whose retrieved window equalled the
    # first run's (the retrieval pin — a flip on an identical window is the
    # reader's).
    repeat_labels: str = ""
    repeat_agreement: float = 1.0
    repeat_window_identical: float = 1.0
    # The item's namespace, so a later pass (latency, re-asks) can query the
    # same ingest without paying for it again (with --keep-namespaces).
    namespace: str = ""
    simplified_hypothesis: str = ""
    simplified_by: str = ""


def _record_usage(sink: dict | None, payload: dict) -> None:
    """Accumulate an OpenAI-style `usage` block into `sink` (tin/tout/calls)."""
    if sink is None or not isinstance(payload, dict):
        return
    u = payload.get("usage") or {}
    sink["tin"] = sink.get("tin", 0) + int(u.get("prompt_tokens") or 0)
    sink["tout"] = sink.get("tout", 0) + int(u.get("completion_tokens") or 0)
    sink["calls"] = sink.get("calls", 0) + 1


# question_id -> namespace from --reuse-csv (an earlier run's CSV with --keep-namespaces).
_REUSE_NAMESPACES: dict[str, str] = {}


def _parse_sweep(spec: str) -> list[int]:
    """'3,5, 10,3' -> [3, 5, 10]: positive ints, de-duplicated, ascending."""
    out: list[int] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            k = int(part)
        except ValueError:
            continue
        if k > 0 and k not in out:
            out.append(k)
    return sorted(out)


def _p50(values: list[int]) -> int:
    if not values:
        return 0
    vals = sorted(values)
    return int(vals[len(vals) // 2])


def _pq(values: list[int], q: float) -> int:
    """Nearest-rank percentile (q in [0, 1]); 0 on empty."""
    if not values:
        return 0
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return int(vals[idx])


def deployment_by_type(results: list) -> list[dict]:
    """One row per question type with the deployment columns beside accuracy (per type):
    search p50, ask p25 / p50 / p75 (the IQR is the spread a deployment plans around, a
    p50 alone hides a bimodal deep pass), mean reader-context tokens
    (`context_tokens_est`) and QA tokens per item: latency and context per LongMemEval
    type."""
    by_t: dict[str, list] = defaultdict(list)
    for r in results:
        by_t[str(getattr(r, "question_type", "") or "")].append(r)
    rows: list[dict] = []
    for qt in sorted(by_t):
        rs = by_t[qt]
        n = len(rs)
        judged = [r for r in rs if getattr(r, "judge_label", "")]
        correct = sum(1 for r in judged if r.judge_label == "CORRECT")
        asks = [int(getattr(r, "ask_latency_ms", 0) or 0) for r in rs]
        asks = [a for a in asks if a > 0]
        ctx = [int(getattr(r, "context_tokens_est", 0) or 0) for r in rs]
        ctx = [c for c in ctx if c > 0]
        rows.append(
            {
                "question_type": qt,
                "n": n,
                "judge": (
                    f"{correct}/{len(judged)} ({(correct / len(judged) * 100.0):.1f}%)"
                    if judged
                    else "—"
                ),
                "search_p50_ms": _p50([int(getattr(r, "search_latency_ms", 0) or 0) for r in rs]),
                "ask_p25_ms": _pq(asks, 0.25),
                "ask_p50_ms": _pq(asks, 0.5),
                "ask_p75_ms": _pq(asks, 0.75),
                "ctx_tokens_mean": round(sum(ctx) / len(ctx)) if ctx else 0,
                "qa_tokens_per_item": (
                    round(sum(r.qa_tokens_in + r.qa_tokens_out for r in rs) / n) if n else 0
                ),
            }
        )
    return rows


def sweep_summary(results: list) -> list[dict]:
    """One row per k of a --top-k-sweep run: recall, judged accuracy, QA
    tokens per item, search and ask p50 latency, deep-pass fire rate — the
    accuracy-versus-tokens curve."""
    by_k: dict[int, list] = defaultdict(list)
    for r in results:
        by_k[int(getattr(r, "sweep_k", 0) or 0)].append(r)
    rows: list[dict] = []
    for k in sorted(by_k):
        rs = by_k[k]
        n = len(rs)
        recalled = sum(1 for r in rs if r.recalled)
        judged = [r for r in rs if r.judge_label]
        correct = sum(1 for r in judged if r.judge_label == "CORRECT")
        qa_tok = sum(r.qa_tokens_in + r.qa_tokens_out for r in rs)
        deep = sum(1 for r in rs if getattr(r, "ask_deep_fired", False))
        rows.append(
            {
                "k": k,
                "n": n,
                "recall": f"{recalled}/{n} ({(recalled / n * 100.0) if n else 0.0:.1f}%)",
                "judge": (
                    f"{correct}/{len(judged)} ({(correct / len(judged) * 100.0):.1f}%)"
                    if judged
                    else "—"
                ),
                "qa_tokens_per_item": round(qa_tok / n) if n else 0,
                "search_p50_ms": _p50([r.search_latency_ms for r in rs]),
                "ask_p50_ms": _p50([getattr(r, "ask_latency_ms", 0) for r in rs]),
                "deep_fired": f"{deep}/{n}" if any(r.answerer == "loom-ask" for r in rs) else "—",
            }
        )
    return rows


def arm_label(arm: dict, index: int) -> str:
    """The row label for one --ask-arms entry: its "label" key, else a compact
    rendering of its fields ("depth=raw"), else its position."""
    label = str(arm.get("label") or "").strip()
    if label:
        return label
    fields = {k: v for k, v in arm.items() if k != "label"}
    if fields:
        return ",".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return f"arm{index}"


def parse_ask_arms(spec: str) -> list[dict]:
    """--ask-arms: a JSON list of memory.ask overrides (each may carry
    "label"). Non-dict entries are dropped; an empty spec is no arms."""
    if not (spec or "").strip():
        return []
    raw = json.loads(spec)
    if isinstance(raw, dict):
        raw = [raw]
    return [a for a in raw if isinstance(a, dict)]


def arm_summary(
    results: list, *, seed: int = 7, gate_args: argparse.Namespace | None = None
) -> list[dict]:
    """One row per arm of an --ask-arms run, paired against the FIRST arm:
    judged accuracy, deep-pass fire rate, ask p50 latency, and the paired
    bootstrap of the per-item judge delta (B minus A) with its 95% lower
    bound — the number the paired gate reads."""
    from bench.bootstrap import paired_bootstrap

    by_arm: dict[str, dict[tuple[str, int], ItemResult]] = defaultdict(dict)
    order: list[str] = []
    for r in results:
        arm = str(getattr(r, "arm", "") or "")
        if arm not in order:
            order.append(arm)
        by_arm[arm][(r.question_id, int(getattr(r, "sweep_k", 0) or 0))] = r
    if not order:
        return []
    base = by_arm[order[0]]
    rows: list[dict] = []
    for arm in order:
        rs = list(by_arm[arm].values())
        judged = [r for r in rs if r.judge_label]
        correct = sum(1 for r in judged if r.judge_label == "CORRECT")
        deep = sum(1 for r in rs if getattr(r, "ask_deep_fired", False))
        row: dict = {
            "arm": arm,
            "n": len(rs),
            # The per-item flip rate over --repeats labels.
            "flip_rate": flip_rate(
                [lb for lb in str(r.repeat_labels or "").split("|") if lb] for r in rs
            ),
            "judge": (
                f"{correct}/{len(judged)} ({(correct / len(judged) * 100.0):.1f}%)"
                if judged
                else "—"
            ),
            "deep_fired": f"{deep}/{len(rs)}",
            "ask_p50_ms": _p50([getattr(r, "ask_latency_ms", 0) for r in rs]),
            "qa_tokens_per_item": round(sum(r.qa_tokens_in + r.qa_tokens_out for r in rs) / len(rs))
            if rs
            else 0,
            "delta": "",
        }
        if arm != order[0]:
            deltas: list[float] = []
            clusters: list[str] = []
            for key, r in by_arm[arm].items():
                a = base.get(key)
                if a is None or not (a.judge_label and r.judge_label):
                    continue
                deltas.append(float(r.judge_label == "CORRECT") - float(a.judge_label == "CORRECT"))
                clusters.append(r.question_type)
            if deltas:
                boot = paired_bootstrap(deltas, clusters, seed=seed)
                row["delta"] = (
                    f"{boot.mean * 100:+.1f}pp [lb {boot.lb * 100:+.1f}] "
                    f"W{boot.wins}/T{boot.ties}/L{boot.losses}"
                    + (" SIGNIFICANT" if boot.significant_gain else "")
                )
            if gate_args is not None and getattr(gate_args, "gate", False):
                # The verdict over MAJORITY labels, flip rate
                # beside it, pass|fail|unresolved against the measured band.
                a_lab = {
                    k: [x for x in str(r.repeat_labels or "").split("|") if x]
                    or [str(r.judge_label or "")]
                    for k, r in base.items()
                    if r.judge_label
                }
                b_lab = {
                    k: [x for x in str(r.repeat_labels or "").split("|") if x]
                    or [str(r.judge_label or "")]
                    for k, r in by_arm[arm].items()
                    if r.judge_label
                }
                g = gate_from_args(
                    gate_args,
                    a_lab,
                    b_lab,
                    clusters={k: r.question_type for k, r in by_arm[arm].items()},
                    seed=seed,
                )
                row["gate"] = g.as_dict()
                row["gate_lines"] = g.lines()
        rows.append(row)
    return rows


def efficiency_summary(results: list) -> dict:
    """Totals and per-item means of the build/QA cost columns,
    in the shape the memory-efficiency literature tabulates: tokens in
    thousands, calls, seconds, and prompt tokens per memory written.
    Build means divide by the items that built a memory: a run with a
    second answering arm over the same histories carries rows that built
    nothing, and counting them would halve the per-history build cost."""
    n = len(results) or 1
    built = sum(1 for r in results if r.build_tokens_in or r.build_tokens_out) or 1
    b_in = sum(r.build_tokens_in for r in results)
    b_out = sum(r.build_tokens_out for r in results)
    b_cached = sum(r.build_tokens_cached for r in results)
    q_in = sum(r.qa_tokens_in for r in results)
    q_out = sum(r.qa_tokens_out for r in results)
    written = sum(r.memories_written for r in results)
    return {
        "items": len(results),
        "build_tokens_k": round((b_in + b_out) / 1000.0, 1),
        "build_tokens_in_k": round(b_in / 1000.0, 1),
        "build_tokens_cached_k": round(b_cached / 1000.0, 1),
        "build_cached_share": round(b_cached / max(1, b_in), 3),
        "build_calls": sum(r.build_calls for r in results),
        "build_segments": sum(r.build_segments for r in results),
        "consolidated_clusters": sum(r.consolidated_clusters for r in results),
        "memories_written": written,
        "tokens_per_memory": round(b_in / max(1, written), 1),
        "build_seconds": round(sum(r.build_seconds for r in results), 1),
        "qa_tokens_k": round((q_in + q_out) / 1000.0, 1),
        "qa_calls": sum(r.qa_calls for r in results),
        "qa_seconds": round(sum(r.qa_seconds for r in results), 1),
        "total_tokens_k": round((b_in + b_out + q_in + q_out) / 1000.0, 1),
        "per_item": {
            "build_tokens_k": round((b_in + b_out) / 1000.0 / built, 2),
            "qa_tokens_k": round((q_in + q_out) / 1000.0 / n, 2),
            "build_seconds": round(sum(r.build_seconds for r in results) / built, 1),
            "qa_seconds": round(sum(r.qa_seconds for r in results) / n, 1),
        },
    }


# Connection-error retries per call (see _post): 8 × up to 30 s ≈ 2 minutes,
# longer than a redeploy's health-check gap.
_CONN_RETRIES = 8
# 429 retries per call, each waiting the server's Retry-After (capped at 60 s).
_RATE_LIMIT_RETRIES = 12


async def _post(
    client: httpx.AsyncClient, path: str, body: dict, token: str = "", *, retries: int = 3
) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Retry transient 5xx with backoff. set_from_messages can intermittently
    # 5xx under high write concurrency (CH write/dedup contention); a dropped
    # seed silently corrupts recall@k, so seeding must be robust for a full run.
    # Connection-level failures (the server restarting under a redeploy: a
    # dropped response, then refused connections for ~30 s) are retried on
    # their own longer schedule — up to ~2 min — so a shared server being
    # restarted mid-run costs latency, not items.
    last: httpx.Response | None = None
    conn_attempts = 0
    rl_attempts = 0
    attempt = 0
    while attempt < retries:
        try:
            r = await client.post(path, json=body, headers=headers, timeout=120.0)
        except (httpx.TransportError, httpx.RemoteProtocolError) as e:
            conn_attempts += 1
            if conn_attempts > _CONN_RETRIES:
                raise
            wait = min(30.0, 3.0 * conn_attempts)
            print(
                f"  … {path.rsplit('/', 1)[-1]}: {type(e).__name__}; "
                f"retry {conn_attempts}/{_CONN_RETRIES} in {wait:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(wait)
            continue
        if r.status_code == 429:
            # Rate-limited: honour Retry-After (loom sets it per namespace
            # bucket) and never count it against the 5xx budget: treating a 429 as
            # terminal loses a bulk ingest's seeds by the thousand.
            rl_attempts += 1
            if rl_attempts > _RATE_LIMIT_RETRIES:
                r.raise_for_status()
            try:
                wait = float(r.headers.get("Retry-After") or 0.0)
            except ValueError:
                wait = 0.0
            wait = min(60.0, max(1.0, wait or 2.0 * rl_attempts))
            print(
                f"  … {path.rsplit('/', 1)[-1]}: 429 rate limited; "
                f"retry {rl_attempts}/{_RATE_LIMIT_RETRIES} in {wait:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(wait)
            continue
        if r.status_code < 500:
            r.raise_for_status()
            return r.json()
        last = r
        attempt += 1
        await asyncio.sleep(0.5 * (2**attempt))
    if last is not None:
        last.raise_for_status()
    raise RuntimeError("unreachable")


async def _llm_synthesize_answer(
    client: httpx.AsyncClient,
    question: str,
    hits: list[dict],
    model: str,
    api_key: str,
    question_date: str = "",
    reader: str = "official",
    usage_sink: dict | None = None,
) -> str:
    """Synthesize an answer from retrieved hits. A SINGLE user message, no
    system prompt (matching the official harness). The completion is the
    hypothesis the judge grades — for the tuned reader we strip the
    <thinking>…</thinking> scratchpad first (only text outside it is the
    answer, per the prompt)."""
    # bench/judges.py shapes the request for the reader's provider and model
    # family (reasoning models get room for their reasoning and no temperature).
    try:
        text, usage = await achat(
            client,
            model,
            _build_answer_user_prompt(question, hits, question_date, reader),
            max_tokens=2048,
            api_key="" if is_claude(model) else api_key,
        )
        _record_usage(usage_sink, {"usage": usage})
        stripped = re.sub(
            r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL | re.IGNORECASE
        ).strip()
        return stripped or text
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        return f"(synthesis error: {e})"


async def _llm_relevance_gate(
    client: httpx.AsyncClient,
    question: str,
    hits: list[dict],
    model: str,
    api_key: str,
    min_keep: int = 3,
    usage_sink: dict | None = None,
) -> tuple[list[dict], str, int]:
    """The server's relevance gate, run bench-side over the reader's hits: one
    JSON verdict from the same GATE_SYSTEM/build_gate_user prompt, folded by
    the same apply_verdict (floor included). Returns (kept, mode, dropped)."""
    n = len(hits)
    if n <= max(1, min_keep):
        return hits, "skipped", 0
    excerpts = [str(h.get("content_excerpt") or "") for h in hits]
    body = {
        "model": model,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": GATE_SYSTEM},
            {"role": "user", "content": build_gate_user(question, excerpts)},
        ],
    }
    relevant: set[int] | None
    try:
        r = await client.post(
            _OPENAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30.0,
        )
        r.raise_for_status()
        _payload = r.json()
        _record_usage(usage_sink, _payload)
        relevant = parse_relevant((_payload["choices"][0]["message"]["content"] or ""), n)
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        relevant = None
    v = apply_verdict(hits, relevant, min_keep=min_keep)
    return list(v.kept), v.mode, len(v.dropped)


async def _simplify_hypothesis(
    client: httpx.AsyncClient,
    question: str,
    hypothesis: str,
    model: str,
    api_key: str,
    usage_sink: dict | None = None,
) -> tuple[str, str]:
    """The published uniform simplification step: reduce the reader's answer to its
    shortest correct string before F1 / BLEU-1. Returns (simplified, simplified_by).
    With no model, the model-free heuristic in bench/answer_scoring.py is used and
    labelled."""
    from bench.answer_scoring import build_simplify_prompt, parse_simplified, simplify_heuristic

    if not model or not api_key:
        return simplify_heuristic(hypothesis), "heuristic"
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 60,
        "messages": [{"role": "user", "content": build_simplify_prompt(question, hypothesis)}],
    }
    try:
        r = await client.post(
            _OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
        r.raise_for_status()
        _payload = r.json()
        _record_usage(usage_sink, _payload)
        reply = (_payload["choices"][0]["message"]["content"] or "").strip()
        return parse_simplified(reply, fallback=hypothesis), f"model:{model}"
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        # A failed call falls back to the heuristic and says so; the row is
        # still scored, never dropped.
        return simplify_heuristic(hypothesis), "heuristic"


def _stale_answer_prompt(question: str, gold: str, earlier: str, response: str) -> str:
    """Was a wrong knowledge-update answer the OUTDATED value? The earlier
    answer session (LongMemEval labels two for every knowledge-update item:
    the superseded statement and the update) is quoted; the judge says yes
    only when the response states what that session said and the gold
    contradicts it."""
    return (
        "A user asked an assistant a question about a fact that CHANGED over time. "
        "Below is the EARLIER conversation (before the change), the CORRECT current "
        "answer, and the assistant's response.\n\n"
        f"Question: {question}\n\n"
        f"Earlier conversation (superseded):\n{earlier}\n\n"
        f"Correct current answer: {gold}\n\n"
        f"Assistant response: {response}\n\n"
        "Does the assistant's response state the OLD value from the earlier "
        "conversation instead of the current one? Answer yes or no only."
    )


async def _stale_answer_check(
    client: httpx.AsyncClient,
    item: dict,
    hypothesis: str,
    model: str,
    api_key: str,
    usage_sink: dict | None = None,
) -> str:
    """ "1" / "0" for a WRONG knowledge-update answer; "" when the earlier
    answer session cannot be located or the call failed. One judge call."""
    sids = [str(x) for x in (item.get("answer_session_ids") or []) if x]
    hs_ids = [str(x) for x in (item.get("haystack_session_ids") or [])]
    if len(sids) < 2 or sids[0] not in hs_ids:
        return ""
    turns = (item.get("haystack_sessions") or [])[hs_ids.index(sids[0])] or []
    earlier = "\n".join(
        f"{str(t.get('role') or '')}: {str(t.get('content') or '')}"
        for t in turns
        if isinstance(t, dict)
    )[:6000]
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 5,
        "messages": [
            {
                "role": "user",
                "content": _stale_answer_prompt(
                    str(item["question"]), str(item["answer"]), earlier, hypothesis
                ),
            }
        ],
    }
    try:
        r = await client.post(
            _OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        if usage_sink is not None:
            u = data.get("usage") or {}
            usage_sink["tin"] = usage_sink.get("tin", 0) + int(u.get("prompt_tokens", 0) or 0)
            usage_sink["tout"] = usage_sink.get("tout", 0) + int(u.get("completion_tokens", 0) or 0)
            usage_sink["calls"] = usage_sink.get("calls", 0) + 1
        reply = str(data["choices"][0]["message"]["content"] or "").strip().lower()
        return "1" if reply.startswith("yes") else "0"
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return ""


async def _llm_judge(
    client: httpx.AsyncClient,
    question: str,
    gold: str,
    generated_answer: str,
    model: str,
    api_key: str,
    question_type: str,
    abstention: bool,
    usage_sink: dict | None = None,
) -> tuple[str | None, str]:
    """Grade the synthesized answer with the OFFICIAL LongMemEval judge
    (evaluate_qa.py::get_anscheck_prompt): per-question-type prompt,
    a 10-token reply, label = 'yes' in the reply. Returns
    ("CORRECT"|"WRONG", raw_reply) so downstream aggregation is unchanged."""
    prompt = _official_anscheck_prompt(
        question_type, question, gold, generated_answer, abstention=abstention
    )
    # A timeout or a 429/5xx is the provider, not the answer: retry with backoff
    # before the caller's ungraded-counts-as-WRONG rule applies. bench/judges.py
    # shapes the request for the judge's provider and model family.
    last = ""
    for attempt in range(_JUDGE_ATTEMPTS):
        try:
            reply, usage = await achat(
                client,
                model,
                prompt,
                max_tokens=10,
                timeout_s=120.0,
                api_key="" if is_claude(model) else api_key,
            )
            _record_usage(usage_sink, {"usage": usage})
            label = "CORRECT" if "yes" in reply.lower() else "WRONG"
            return label, reply
        except httpx.HTTPStatusError as e:
            last = f"{e.response.status_code} {e}"
            if e.response.status_code != 429 and e.response.status_code < 500:
                break
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        await asyncio.sleep(2**attempt)
    return None, f"judge error: {last}"


async def _run_one_item(
    client: httpx.AsyncClient,
    base_url: str,
    item: dict,
    top_k: int,
    sem: asyncio.Semaphore,
    judge_model: str | None = None,
    reader_model: str = _DEFAULT_READER_MODEL,
    judge_api_key: str = "",
    token: str = "",
    no_extraction_instructions: bool = False,
    consolidate: bool = False,
    sessions_per_call: int = 1,
    ingest_json: dict | None = None,
    search_mode: str = "bm25_hybrid",
    search_json: dict | None = None,
    rerank: str = "builtin:openai",
    pool_unmatched: int = 120,
    ingest_concurrency: int = 8,
    reader: str = "official",
    answerer: str = "reader",
    ask_json: dict | None = None,
    sweep_ks: list[int] | None = None,
    relevance_gate: bool = False,
    gate_model: str = "",
    ask_arms: list[dict] | None = None,
    gate_kind: str = "judge",
    knee_floor: float = 0.75,
    lexical_scores: bool = False,
    simplify_model: str = "",
    capture_consent: bool = True,
    stale_check: bool = False,
    namespace_json: dict | None = None,
    keep_namespaces: bool = False,
    repeats: int = 1,
    credit: bool = False,
) -> list[ItemResult]:
    """Ingest one item once, then answer its question at each k in `sweep_ks`
    (default: just `top_k`) and, with `ask_arms`, under each memory.ask
    configuration (paired: same ingest, same k). Returns one ItemResult per
    (k, arm); build columns ride the first row only."""
    async with sem:
        # --reuse-csv: the question's namespace from an earlier --keep-namespaces
        # run is answered again with no ingest (recall columns are that run's).
        reuse_ns = _REUSE_NAMESPACES.get(str(item.get("question_id", "")), "")
        namespace = reuse_ns or f"lme-{uuid.uuid4().hex[:10]}"
        if not reuse_ns:
            remember_bench_namespace("dev", namespace)  # erased when the run exits
        identity = {
            "org": "dev",
            "namespace": namespace,
            "agent": "lme-bench",
            "user_id": "-",
        }

        # Registered as bench before the first write (bench/bench_namespaces.py),
        # with query capture unless --no-capture-consent: otherwise its
        # search_trace_log rows carry a blank query and the relied-on flywheel
        # cannot use them. --keep-namespaces keeps it out of the idle gc.
        if not reuse_ns:
            await register_bench_namespace(
                lambda path, body: _post(client, base_url + path, body, token),
                org=identity["org"],
                namespace=namespace,
                description=f"LongMemEval item {item.get('question_id', '')}",
                capture=capture_consent,
                keep=keep_namespaces,
                extra=namespace_json or None,
            )

        haystack_sessions = item.get("haystack_sessions", []) or []
        haystack_dates = item.get("haystack_dates", []) or []
        # Per-session ids. Stamped onto each write's session_id, and used to
        # build a memory_key -> {evidence sessions} map from the set_from_messages
        # `written` responses, enabling evidence-session recall@k (the standard
        # LongMemEval retrieval metric) without relying on hit provenance fields.
        haystack_session_ids = item.get("haystack_session_ids", []) or []
        key_to_sessions: dict[str, set[str]] = {}
        build = {
            "tin": 0,
            "tout": 0,
            "tcached": 0,
            "calls": 0,
            "written": 0,
            "segments": 0,
            "consolidated": 0,
        }
        _t_build0 = time.perf_counter()

        # Indexing: one memory.set_from_messages per SESSION — the natural
        # unit (the benchmark's "indexing" step converts each session to KV
        # items) and the only tractable granularity on -S (a question has
        # ~40-50 haystack sessions; pairwise extraction would be ~10x the
        # calls → ~250k for the full set). The whole session's messages go
        # in one call so the extractor sees full intra-session context.
        # Narrative-style extraction tuning for LongMemEval. The
        # eval scores noticeably better on single-session-preference
        # questions (gold answers are paragraph-form descriptions of
        # user preferences) when the extraction prompt produces
        # "self-contained, contextually rich factual statements."
        # Loom's default extraction is more atomic, so the bench
        # passes a per-call instruction telling the extractor to
        # produce richer, narrative-flavoured memories from
        # conversational input.
        narrative_instructions = (
            "When extracting facts from conversation messages, produce "
            "memories that are SELF-CONTAINED AND CONTEXTUALLY RICH. "
            "Each memory's value should read like a complete short "
            "narrative sentence that captures the user's specific "
            "context, motivation, and any relevant qualifiers — not "
            "just an atomic predicate. For preferences, include WHY "
            "the preference exists when stated (e.g. 'User prefers "
            "northern Italian dishes because they grew up in Milan' "
            "not 'User likes Italian food'). For decisions, include "
            "the trade-off the user weighed. For ongoing situations, "
            "include the user's emotional state or motivation when "
            "expressed. Atomic terse facts are LOWER quality on this "
            "workload — narrative depth is the lift. ALSO capture what "
            "the ASSISTANT supplied in reply: the exact name, value, "
            "number, date, quote, or recommendation it gave — users "
            "later ask the assistant to recall what it said or "
            "recommended."
        )
        # Ingest sessions CONCURRENTLY within the item (bounded). Each
        # set_from_messages runs LLM extraction server-side (~10-15s), so a
        # question's ~40-50 sessions done sequentially would be ~12 min/item
        # (hours for the full set). A per-item semaphore caps in-flight
        # writes. key_to_sessions mutation is safe under asyncio — there's no
        # await between setdefault and add, so coroutines can't interleave it.
        ingest_sem = asyncio.Semaphore(max(1, ingest_concurrency))

        async def _ingest(i: int, session: list) -> None:
            if not session:
                return
            session_date = _to_iso_date(haystack_dates[i]) if i < len(haystack_dates) else ""
            sid = (
                str(haystack_session_ids[i])
                if i < len(haystack_session_ids) and haystack_session_ids[i]
                else ""
            )
            body: dict = {**identity, "messages": session, "max_tokens": 4096}
            if ingest_json:
                body.update(ingest_json)  # e.g. {"detect_updates": true} for an A/B
            if sid:
                body["session_id"] = sid
            if not no_extraction_instructions:
                body["extraction_instructions"] = narrative_instructions
            if session_date:
                body["observation_date"] = session_date
            async with ingest_sem:
                seed_resp = await _post(
                    client, base_url + "/v1/memory.set_from_messages", body, token
                )
            build["calls"] += 1
            build["segments"] += int(seed_resp.get("segments") or 1)
            build["tin"] += int(seed_resp.get("tokens_in") or 0)
            build["tout"] += int(seed_resp.get("tokens_out") or 0)
            build["tcached"] += int(seed_resp.get("cached_tokens_in") or 0)
            build["written"] += len(seed_resp.get("written") or [])
            for w in seed_resp.get("written") or []:
                key = str(w.get("memory_key") or "")
                if key:
                    key_to_sessions.setdefault(key, set()).add(sid)

        if reuse_ns:
            pass  # memorized by the earlier run
        elif sessions_per_call > 1:
            # Group consecutive sessions into one call: the messages are
            # concatenated, the call's session_id is the first session's, and
            # every grouped session id is credited for the memories written
            # (evidence recall stays computable). Dates: the first session's.
            groups: list[list[int]] = []
            for i in range(0, len(haystack_sessions), sessions_per_call):
                groups.append(list(range(i, min(i + sessions_per_call, len(haystack_sessions)))))

            async def _ingest_group(idxs: list[int]) -> None:
                merged: list = []
                for i in idxs:
                    merged.extend(haystack_sessions[i] or [])
                if not merged:
                    return
                first = idxs[0]
                session_date = (
                    _to_iso_date(haystack_dates[first]) if first < len(haystack_dates) else ""
                )
                sids = [
                    str(haystack_session_ids[i])
                    for i in idxs
                    if i < len(haystack_session_ids) and haystack_session_ids[i]
                ]
                body: dict = {**identity, "messages": merged[:400], "max_tokens": 4096}
                if sids:
                    body["session_id"] = sids[0]
                if not no_extraction_instructions:
                    body["extraction_instructions"] = narrative_instructions
                if session_date:
                    body["observation_date"] = session_date
                async with ingest_sem:
                    seed_resp = await _post(
                        client, base_url + "/v1/memory.set_from_messages", body, token
                    )
                build["calls"] += 1
                build["segments"] += int(seed_resp.get("segments") or 1)
                build["tin"] += int(seed_resp.get("tokens_in") or 0)
                build["tout"] += int(seed_resp.get("tokens_out") or 0)
                build["tcached"] += int(seed_resp.get("cached_tokens_in") or 0)
                build["written"] += len(seed_resp.get("written") or [])
                for w in seed_resp.get("written") or []:
                    key = str(w.get("memory_key") or "")
                    if key:
                        for sid in sids:
                            key_to_sessions.setdefault(key, set()).add(sid)

            await asyncio.gather(*(_ingest_group(g) for g in groups))
        else:
            await asyncio.gather(*(_ingest(i, s) for i, s in enumerate(haystack_sessions)))
        if consolidate and not reuse_ns:
            # Sleep-time update between build and QA: merge
            # what the session-level extractions fragmented, then answer from
            # the consolidated store. Its LLM tokens count on the build side.
            cons = await _post(
                client,
                base_url + "/v1/memory.consolidate",
                {
                    **identity,
                    "min_age_seconds": 0,
                    "limit": 1000,
                    "dry_run": False,
                },
                token,
            )
            build["calls"] += 1
            build["tin"] += int(cons.get("tokens_in") or 0)
            build["tout"] += int(cons.get("tokens_out") or 0)
            build["consolidated"] = len(cons.get("consolidated") or [])
        build_seconds = time.perf_counter() - _t_build0

        ks = [int(k) for k in (sweep_ks or [top_k]) if int(k) > 0] or [top_k]

        # An arm may also name its own answerer, reader model and top_k, so one
        # ingest grades memory.ask and a pinned external reader side by side.
        _arm_meta = ("label", "answerer", "reader_model", "top_k")
        arms: list[tuple[str, dict | None, dict]] = (
            [
                (
                    arm_label(a, j),
                    {k: v for k, v in a.items() if k not in _arm_meta},
                    {k: a[k] for k in _arm_meta[1:] if k in a},
                )
                for j, a in enumerate(ask_arms)
            ]
            if ask_arms
            else [("", ask_json, {})]
        )

        async def _qa(
            effective_top_k: int, arm: tuple[str, dict | None, dict], first: bool
        ) -> ItemResult:
            meta = arm[2]
            qa_usage = {"tin": 0, "tout": 0, "calls": 0}
            _t_qa0 = time.perf_counter()
            return await _answer_at_k(
                client,
                base_url,
                item,
                identity,
                int(meta.get("top_k") or effective_top_k),
                key_to_sessions,
                qa_usage,
                _t_qa0,
                build=build if first else None,
                build_seconds=build_seconds if first else 0.0,
                judge_model=judge_model,
                reader_model=str(meta.get("reader_model") or reader_model),
                judge_api_key=judge_api_key,
                token=token,
                search_mode=search_mode,
                search_json=search_json,
                rerank=rerank,
                pool_unmatched=pool_unmatched,
                reader=reader,
                answerer=str(meta.get("answerer") or answerer),
                ask_json=arm[1],
                sweep_k=effective_top_k if sweep_ks else 0,
                arm=arm[0],
                relevance_gate=relevance_gate,
                gate_model=gate_model,
                gate_kind=gate_kind,
                knee_floor=knee_floor,
                lexical_scores=lexical_scores,
                stale_check=stale_check,
                simplify_model=simplify_model,
                credit=credit,
            )

        out: list[ItemResult] = []
        first = True
        for k in ks:
            for arm in arms:
                runs = [await _qa(k, arm, first=first and i == 0) for i in range(max(1, repeats))]
                out.append(fold_repeats(runs))
                first = False
        return out


async def _answer_at_k(
    client: httpx.AsyncClient,
    base_url: str,
    item: dict,
    identity: dict,
    top_k: int,
    key_to_sessions: dict[str, set[str]],
    qa_usage: dict,
    _t_qa0: float,
    *,
    build: dict | None,
    build_seconds: float,
    judge_model: str | None,
    reader_model: str,
    judge_api_key: str,
    token: str,
    search_mode: str,
    search_json: dict | None,
    rerank: str,
    pool_unmatched: int,
    reader: str,
    answerer: str,
    ask_json: dict | None,
    sweep_k: int,
    relevance_gate: bool = False,
    gate_model: str = "",
    arm: str = "",
    credit: bool = False,
    gate_kind: str = "judge",
    knee_floor: float = 0.75,
    lexical_scores: bool = False,
    stale_check: bool = False,
    simplify_model: str = "",
) -> ItemResult:
    """The question stage for one already-ingested item at one k: search
    (recall@k), answer (external reader or server-side memory.ask), judge."""
    # Search for the question. Enable the built-in OpenAI reranker
    # per request — bench namespaces are ephemeral so we don't pre-
    # register them with rerank_endpoint='builtin:openai'.
    # No bench-side query classification — the server's LLM-judged
    # query enrichment (preference_attribute axis) decides what
    # this question wants; a regex here would be the anti-pattern the
    # server avoids.
    question_str = str(item["question"])
    effective_top_k = top_k
    # Lexical columns; zero / empty unless --lexical-scores.
    simplified_hypothesis = ""
    simplified_by = ""
    f1_score = 0.0
    bleu1_score = 0.0
    rouge_l_f1_score = 0.0
    rouge_l_recall_score = 0.0

    # "oracle-routed" picks the retrieval mode from the dataset's GOLD
    # question_type — an oracle-router ablation, useful for isolating the
    # specialized modes (temporal/graph). Production callers don't know
    # the category of an incoming query, so headline numbers must come
    # from a fixed mode (default "rrf": the server planner routes every
    # query itself).
    effective_search_mode = search_mode
    if search_mode == "oracle-routed":
        effective_search_mode = {
            "temporal-reasoning": "temporal",
            "multi-session": "graph",
        }.get(str(item.get("question_type", "")), "rrf")

    # Pass the question's "as of" date so temporal phrases in the
    # question ("Valentine's day", "last Tuesday", "two weeks ago")
    # resolve to ISO dates server-side via expand_temporal_query.
    # Production callers know the current date; the bench has to
    # supply it explicitly. LongMemEval items carry question_date
    # in the same format as haystack_dates.
    question_iso = _to_iso_date(str(item.get("question_date", "")))
    # search_mode default is bm25_hybrid — IDF-weighted BM25 fused
    # with cosine, plus entity_boost from graph_seeds. Recovers
    # named-entity recall that pure semantic drops (e.g. "the deli
    # near the Vatican" → "Roscioli" needs the lexical leg). The
    # --search-mode CLI flag exposes the other server-side modes
    # for ablation: 'semantic' (cosine only), 'lexical' (BM25 only),
    # 'hybrid' (cosine + lexical without IDF), 'graph' (entity-walk).
    # --rerank flag controls the cross-encoder rerank: 'builtin:openai'
    # (LLM reorder), 'none' (no rerank, raw vector order).
    # --pool-unmatched controls pre-rerank pool width. Research:
    # pool-K is the single biggest recall lever — cross-encoder rerank
    # is precision, not recall, so it can't recover documents the
    # first stage missed.
    search_body: dict = {
        **identity,
        "query": question_str,
        "top_k": effective_top_k,
        "search_mode": effective_search_mode,
        "alpha": 0.5,
        "include_top_n_unmatched": pool_unmatched,
        # Pin scope to the per-item namespace. The server default
        # ("all_mine") resolves the caller's namespaces from the
        # grants table when the JWT carries a wildcard claim — a
        # single unrelated grant row then silently redirects every
        # search away from the namespace this item just ingested
        # into, zeroing recall with no error surfaced.
        "namespace_scope": "this",
    }
    if rerank and rerank != "none":
        search_body["rerank"] = rerank
    if question_iso:
        search_body["observation_date"] = question_iso
    # Arbitrary per-request overrides for A/B without a redeploy, e.g.
    # '{"search_mode":"graph"}'. An evaluation read credits no recall unless
    # --credit.
    search_body = {
        **search_body,
        **measurement_fields("search", credit=credit),
        **(search_json or {}),
    }
    import time as _time

    _search_t0 = _time.perf_counter()
    resp = await _post(
        client,
        base_url + "/v1/memory.search",
        search_body,
        token,
    )
    _search_latency_ms = int((_time.perf_counter() - _search_t0) * 1000)
    hits = resp.get("results", [])
    excerpts = [h.get("content_excerpt", "") for h in hits]

    # Recall@k = did retrieval surface a memory from one of the labelled
    # gold evidence sessions? (the standard LongMemEval retrieval metric).
    # Hits carry source_resource_id="memory.set_from_messages:<sid>:<run>",
    # so we match the retrieved sessions against answer_session_ids — this
    # is robust to paraphrase, unlike the old substring-of-gold-answer
    # check (which read 0% for computed/synthesised answers even when the
    # right evidence was retrieved).
    answer_sessions = {str(s) for s in (item.get("answer_session_ids") or []) if s}
    retrieved_sessions: set[str] = set()
    for h in hits[:effective_top_k]:
        retrieved_sessions |= key_to_sessions.get(str(h.get("memory_key") or ""), set())
    retrieved_sessions.discard("")
    # Fact-level diagnostic: did the gold answer text actually surface in a
    # retrieved excerpt? Meaningful for STATED-fact categories (e.g.
    # single-session-assistant: gold "Roscioli" should appear if the
    # assistant fact was extracted+retrieved); ~0 for computed answers
    # (temporal diffs / counts) by design.
    gold_norm = _norm(str(item["answer"]))
    answer_in_context = any(gold_norm and gold_norm in _norm(e) for e in excerpts)
    if answer_sessions:
        recalled = bool(answer_sessions & retrieved_sessions)
    else:
        # Items without labelled evidence sessions: fall back to substring.
        recalled = answer_in_context

    # Optional LLM-judge grading. Two-step pipeline matching the
    # published methodology: (1) synthesize an answer from retrieved
    # memories, (2) judge that answer vs gold. Without (1), atomic-
    # fact retrieval is graded against paragraph-form golds and
    # always fails — apples-to-oranges against the published end-
    # to-end scores.
    judge_label: str | None = None
    judge_rationale = ""
    hypothesis = ""
    stale_answer = ""
    ask_depth = ""
    ask_deep_fired = False
    ask_route_tier = ""
    cited_session_jaccard = -1.0
    ask_latency_ms = 0
    context_tokens_est = 0
    gate_mode, gate_kept, gate_dropped = "", 0, 0
    gate_aic = False
    gate_judge_label: str | None = None
    gate_hypothesis = ""
    if judge_model and judge_api_key:
        if answerer == "loom-ask":
            # Server-side synthesis: the instrument under test is loom's
            # own reader (memory.ask), so this arm is NOT comparable to the
            # pinned-reader headline — it compares memory.ask configurations
            # (depth, cascade) against each other under one synth model.
            ask_body: dict = {
                **identity,
                "query": question_str,
                "top_k": effective_top_k,
                "search_mode": effective_search_mode,
                "namespace_scope": "this",
                "allow_synthesis": True,
            }
            if question_iso:
                # The question's date, so "two months ago" resolves against the
                # corpus's clock and not the wall clock (same as the search body).
                ask_body["observation_date"] = question_iso
            ask_body.update(measurement_fields("ask", credit=credit))
            if ask_json:
                ask_body.update(ask_json)
            _ask_t0 = _time.perf_counter()
            ask_resp = await _post(client, base_url + "/v1/memory.ask", ask_body, token)
            ask_latency_ms = int((_time.perf_counter() - _ask_t0) * 1000)
            qa_usage["calls"] += 1
            hypothesis = str(ask_resp.get("answer") or "").strip()
            if not hypothesis:
                hypothesis = "The stored memories do not contain the answer to this question."
            ask_depth = str(ask_resp.get("depth") or "")
            ask_deep_fired = bool(ask_resp.get("deep_fired"))
            ask_route_tier = str(ask_resp.get("route_tier") or "")
            context_tokens_est = int(ask_resp.get("context_tokens_est") or 0)
            _cited = {str(x) for x in (ask_resp.get("cited_sessions") or []) if x}
            if _cited or answer_sessions:
                cited_session_jaccard = (
                    len(_cited & answer_sessions) / len(_cited | answer_sessions)
                    if (_cited | answer_sessions)
                    else 1.0
                )
        else:
            hypothesis = await _llm_synthesize_answer(
                client,
                str(item["question"]),
                hits,  # full hits with temporal_anchor / valid_at metadata
                reader_model,
                judge_api_key,
                question_date=str(item.get("question_date", "")),
                reader=reader,
                usage_sink=qa_usage,
            )
        judge_label, judge_rationale = await _llm_judge(
            client,
            str(item["question"]),
            str(item["answer"]),
            hypothesis,
            judge_model,
            judge_api_key,
            question_type=str(item.get("question_type", "")),
            abstention="_abs" in str(item.get("question_id", "")),
            usage_sink=qa_usage,
        )
        if lexical_scores:
            from bench.answer_scoring import bleu1 as _bleu1
            from bench.answer_scoring import rouge_l as _rouge_l
            from bench.answer_scoring import token_f1 as _token_f1

            simplified_hypothesis, simplified_by = await _simplify_hypothesis(
                client,
                str(item["question"]),
                hypothesis,
                simplify_model,
                judge_api_key,
                usage_sink=qa_usage,
            )
            f1_score = _token_f1(simplified_hypothesis, str(item["answer"]))
            bleu1_score = _bleu1(simplified_hypothesis, str(item["answer"]))
            rouge_l_f1_score, rouge_l_recall_score = _rouge_l(
                simplified_hypothesis, str(item["answer"])
            )
        if relevance_gate and answerer != "loom-ask":
            # Paired arm: identical hits through the server's gate prompt,
            # then the same reader + judge. The delta vs judge_label is the
            # gate's worth on this item with retrieval held fixed. (memory.ask
            # has no judge gate, so the arm is reader-side only.)
            if gate_kind == "knee":
                # LLM-free arm: the server's similarity_floor_cut
                # over the same hits — membership by cosine relative to the top.
                _knee = similarity_floor_cut(hits, rel_floor=knee_floor, min_keep=3)
                gated_hits, gate_mode, gate_dropped = (
                    list(_knee.kept),
                    _knee.mode,
                    len(_knee.dropped),
                )
            else:
                gated_hits, gate_mode, gate_dropped = await _llm_relevance_gate(
                    client,
                    str(item["question"]),
                    hits,
                    gate_model or reader_model,
                    judge_api_key,
                    usage_sink=qa_usage,
                )
            gate_kept = len(gated_hits)
            gate_aic = any(
                gold_norm and gold_norm in _norm(h.get("content_excerpt", "") or "")
                for h in gated_hits
            )
            gate_hypothesis = await _llm_synthesize_answer(
                client,
                str(item["question"]),
                gated_hits,
                reader_model,
                judge_api_key,
                question_date=str(item.get("question_date", "")),
                reader=reader,
                usage_sink=qa_usage,
            )
            gate_judge_label, _ = await _llm_judge(
                client,
                str(item["question"]),
                str(item["answer"]),
                gate_hypothesis,
                judge_model,
                judge_api_key,
                question_type=str(item.get("question_type", "")),
                abstention="_abs" in str(item.get("question_id", "")),
                usage_sink=qa_usage,
            )
        if judge_label is None:
            # A failed judge call must not shrink the judge denominator —
            # an ungraded answer counts against, never as a dropped item.
            judge_label = "WRONG"
        if stale_check and str(item.get("question_type", "")) == "knowledge-update":
            stale_answer = (
                await _stale_answer_check(
                    client, item, hypothesis, judge_model, judge_api_key, usage_sink=qa_usage
                )
                if judge_label == "WRONG"
                else "0"
            )

    return ItemResult(
        namespace=str(identity.get("namespace") or ""),
        question_id=str(item["question_id"]),
        question_type=str(item["question_type"]),
        question=str(item["question"]),
        gold_answer=str(item["answer"]),
        top_k_excerpts=excerpts,
        recalled=recalled,
        answer_in_context=answer_in_context,
        judge_label=judge_label,
        judge_rationale=judge_rationale,
        stale_answer=stale_answer,
        hypothesis=hypothesis,
        graph_seeds=int(resp.get("graph_seeds", 0) or 0),
        graph_neighbors_added=int(resp.get("graph_neighbors_added", 0) or 0),
        triples_extracted_from_query=int(resp.get("triples_extracted_from_query", 0) or 0),
        triples_matched=int(resp.get("triples_matched", 0) or 0),
        search_mode=effective_search_mode,
        rerank=rerank,
        pool_unmatched=int(pool_unmatched),
        search_latency_ms=_search_latency_ms,
        build_tokens_in=build["tin"] if build else 0,
        build_tokens_out=build["tout"] if build else 0,
        build_tokens_cached=build["tcached"] if build else 0,
        build_calls=build["calls"] if build else 0,
        build_segments=build["segments"] if build else 0,
        consolidated_clusters=build["consolidated"] if build else 0,
        memories_written=build["written"] if build else 0,
        build_seconds=round(build_seconds, 3),
        qa_tokens_in=qa_usage["tin"],
        qa_tokens_out=qa_usage["tout"],
        qa_calls=qa_usage["calls"] + 1,  # + the memory.search call
        qa_seconds=round(time.perf_counter() - _t_qa0, 3),
        sweep_k=sweep_k,
        answerer=answerer,
        ask_depth=ask_depth,
        ask_deep_fired=ask_deep_fired,
        ask_route_tier=ask_route_tier,
        cited_session_jaccard=round(cited_session_jaccard, 4),
        ask_latency_ms=ask_latency_ms,
        context_tokens_est=context_tokens_est,
        gate_mode=gate_mode,
        gate_kept=gate_kept,
        gate_dropped=gate_dropped,
        gate_answer_in_context=gate_aic,
        gate_judge_label=gate_judge_label,
        gate_hypothesis=gate_hypothesis,
        arm=arm,
        variant=str(item.get("variant", "") or ""),
        f1=round(f1_score, 4),
        bleu1=round(bleu1_score, 4),
        rouge_l_f1=round(rouge_l_f1_score, 4),
        rouge_l_recall=round(rouge_l_recall_score, 4),
        simplified_hypothesis=simplified_hypothesis,
        simplified_by=simplified_by,
    )


def fold_repeats(runs: list[ItemResult]) -> ItemResult:
    """One row from N answers of the same (item, k, arm): the first run's
    fields, `judge_label` replaced by the MAJORITY (None stays
    None when no run was judged), every label and the window pin recorded."""
    base = runs[0]
    if len(runs) == 1:
        return base
    labels = [str(r.judge_label or "") for r in runs if r.judge_label]
    if labels:
        base.judge_label = majority_label(labels)
        base.repeat_labels = "|".join(labels)
        base.repeat_agreement = agreement(labels)
    base.repeat_window_identical = identical_share([tuple(r.top_k_excerpts) for r in runs])
    return base


def _load_dataset(path: Path) -> list[dict]:
    with path.open() as f:
        return json.load(f)


def _write_csv(path: Path, results: list[ItemResult]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "question_id",
                "question_type",
                "question",
                "gold_answer",
                "recalled",
                "judge_label",
                "stale_answer",
                "judge_rationale",
                "graph_seeds",
                "graph_neighbors_added",
                "triples_extracted_from_query",
                "triples_matched",
                # Run-config columns for matrix-runner attribution.
                "search_mode",
                "rerank",
                "pool_unmatched",
                "search_latency_ms",
                "build_tokens_in",
                "build_tokens_out",
                "build_tokens_cached",
                "build_calls",
                "build_segments",
                "consolidated_clusters",
                "memories_written",
                "build_seconds",
                "qa_tokens_in",
                "qa_tokens_out",
                "qa_calls",
                "qa_seconds",
                "context_tokens_est",
                "answer_in_context",
                "gate_mode",
                "gate_kept",
                "gate_dropped",
                "gate_answer_in_context",
                "gate_judge_label",
                "sweep_k",
                "answerer",
                "ask_depth",
                "ask_deep_fired",
                "ask_route_tier",
                "cited_session_jaccard",
                "ask_latency_ms",
                "arm",
                "f1",
                "bleu1",
                "rouge_l_f1",
                "rouge_l_recall",
                "simplified_hypothesis",
                "simplified_by",
                "variant",
                "repeat_labels",
                "repeat_agreement",
                "repeat_window_identical",
                "namespace",
            ]
        )
        for r in results:
            w.writerow(
                [
                    r.question_id,
                    r.question_type,
                    r.question,
                    r.gold_answer,
                    "1" if r.recalled else "0",
                    r.judge_label or "",
                    r.stale_answer,
                    r.judge_rationale,
                    r.graph_seeds,
                    r.graph_neighbors_added,
                    r.triples_extracted_from_query,
                    r.triples_matched,
                    r.search_mode,
                    r.rerank,
                    r.pool_unmatched,
                    r.search_latency_ms,
                    r.build_tokens_in,
                    r.build_tokens_out,
                    r.build_tokens_cached,
                    r.build_calls,
                    r.build_segments,
                    r.consolidated_clusters,
                    r.memories_written,
                    r.build_seconds,
                    r.qa_tokens_in,
                    r.qa_tokens_out,
                    r.qa_calls,
                    r.qa_seconds,
                    r.context_tokens_est,
                    "1" if r.answer_in_context else "0",
                    r.gate_mode,
                    r.gate_kept,
                    r.gate_dropped,
                    "1" if r.gate_answer_in_context else "0",
                    r.gate_judge_label or "",
                    r.sweep_k,
                    r.answerer,
                    r.ask_depth,
                    "1" if r.ask_deep_fired else "0",
                    r.ask_route_tier,
                    r.cited_session_jaccard,
                    r.ask_latency_ms,
                    r.arm,
                    r.f1,
                    r.bleu1,
                    r.rouge_l_f1,
                    r.rouge_l_recall,
                    r.simplified_hypothesis,
                    r.simplified_by,
                    r.variant,
                    r.repeat_labels,
                    r.repeat_agreement,
                    r.repeat_window_identical,
                    r.namespace,
                ]
            )


async def main() -> int:
    import random

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument(
        "--dataset",
        default="bench/datasets/longmemeval_s_cleaned.json",
        help="Default is the real-haystack -S split (~47 sessions/item). "
        "Pass bench/datasets/longmemeval_oracle.json for the cheap "
        "evidence-only split (~2 sessions/item; near-zero retrieval "
        "pressure — not comparable to -S numbers).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Cap on items processed (cost control). Pass 0 for the full 500.",
    )
    # The published LongMemEval benchmark runs at --top_k 30 (their
    # library default is 10, but the eval scripts use 30). We match
    # exactly for apples-to-apples comparison; the judge gets 3× the
    # candidate pool to find supporting context in.
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument(
        "--ingest-json",
        default="",
        help="JSON object merged into every memory.set_from_messages body — "
        "per-request write overrides for an A/B, e.g. '{\"detect_updates\":true}'",
    )
    parser.add_argument(
        "--search-json",
        default="",
        help="JSON object merged into every memory.search body — per-request "
        "overrides for an A/B, e.g. "
        '\'{"search_mode":"graph"}\'',
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Concurrent items in flight. Each item is sequential per-session internally.",
    )
    parser.add_argument("--out", default="bench/longmemeval.csv")
    parser.add_argument(
        "--hyp-out",
        default="",
        help="Write a hypotheses JSONL ({question_id, hypothesis}) for the "
        "official evaluate_qa.py cross-check. Defaults to <out>.hyp.jsonl when "
        "--answer-judge is set.",
    )
    parser.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--evidence-position",
        choices=["", "early", "middle", "late"],
        default="",
        help="Move each item's evidence sessions into this third of the haystack "
        "before ingest. '' = leave the dataset order.",
    )
    parser.add_argument(
        "--context-scale",
        type=float,
        default=1.0,
        help="Scale each item's haystack: <1 drops non-evidence sessions, >1 pads with "
        "other items' non-evidence sessions (ids prefixed pad:). Evidence is never dropped.",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Shuffle the dataset before --limit so the "
        "sample isn't all one question_type "
        "(LongMemEval is sorted by category).",
    )
    parser.add_argument(
        "--answer-judge",
        action="store_true",
        help="In addition to strict-recall, synthesize an answer with the "
        "pinned reader and grade it with the official LongMemEval judge. "
        "Requires OPENAI_API_KEY in env.",
    )
    parser.add_argument(
        "--reader-model",
        default=_DEFAULT_READER_MODEL,
        help="Answer-synthesis model (produces the hypothesis the judge "
        "grades). Pinned so the instrument doesn't move between runs; "
        "override via BENCH_READER_MODEL or this flag.",
    )
    parser.add_argument(
        "--judge-model",
        default=_DEFAULT_JUDGE_MODEL,
        help="Judge model. Defaults to the official LongMemEval snapshot "
        f"({_OFFICIAL_JUDGE_MODEL}); BENCH_JUDGE_MODEL or this flag "
        "overrides. A non-official judge prints a comparability warning.",
    )
    parser.add_argument(
        "--sessions-per-call",
        type=int,
        default=1,
        help="Concatenate this many haystack sessions into ONE set_from_messages "
        "call (default 1 = one call per session). >1 mixes topics in a call the "
        "way a long unattended session does.",
    )
    parser.add_argument(
        "--consolidate",
        action="store_true",
        help="Run memory.consolidate on each item's namespace after ingest and "
        "before QA: the before/after check that offline "
        "merging does not lose answers. Merge tokens count on the build side.",
    )
    parser.add_argument(
        "--no-extraction-instructions",
        action="store_true",
        help="Disable the LongMemEval-tuned "
        "narrative_instructions passed to "
        "set_from_messages. Reports Loom's product "
        "default (no workload-specific tuning).",
    )
    parser.add_argument(
        "--search-mode",
        default="rrf",
        choices=[
            "rrf",
            "oracle-routed",
            "semantic",
            "lexical",
            "hybrid",
            "bm25_hybrid",
            "graph",
            "temporal",
        ],
        help="Server-side search mode. Default 'rrf' lets the server planner "
        "route every query — the production path, so it's the headline "
        "number. 'oracle-routed' picks the mode from the dataset's GOLD "
        "question_type (temporal-reasoning→temporal, multi-session→graph, "
        "else rrf) — an oracle-router ablation, since production callers "
        "don't know the category. Pass a single mode to ablate (e.g. "
        "bm25_hybrid).",
    )
    parser.add_argument(
        "--rerank",
        default="builtin:openai",
        help="Cross-encoder rerank endpoint. 'none' disables rerank "
        "entirely (raw vector order). Ablation: measure how much "
        "rerank contributes to judge above raw retrieval.",
    )
    parser.add_argument(
        "--pool-unmatched",
        type=int,
        default=120,
        help="Pre-rerank pool width — how many additional candidates "
        "beyond top_k to feed the reranker. Pool-K is the single "
        "biggest recall lever; default 120 → total pool ~150 at top_k=30.",
    )
    parser.add_argument(
        "--reader",
        default="official",
        choices=["official", "tuned"],
        help="Answer-generation reader. 'official' = the run_generation.py "
        "prompt verbatim (untuned, defensible default). 'tuned' = a "
        "verbose-reasoning posture (disclosed bench tuning) kept as an "
        "ablation to show reader headroom. Judge stays official either way.",
    )
    parser.add_argument(
        "--ingest-concurrency",
        type=int,
        default=8,
        help="Concurrent set_from_messages calls PER ITEM. Each runs ~10-15s "
        "of server-side LLM extraction, and a -S item has ~40-50 sessions, so "
        "sequential ingestion is ~12 min/item. Total in-flight writes ≈ "
        "--concurrency × --ingest-concurrency; dial down if OpenAI rate-limits.",
    )
    parser.add_argument(
        "--answerer",
        default="reader",
        choices=["reader", "loom-ask"],
        help="Who writes the hypothesis the judge grades. 'reader' = the pinned "
        "external reader over memory.search hits (the comparable headline). "
        "'loom-ask' = server-side memory.ask synthesis; use it to A/B "
        'memory.ask configurations (e.g. --ask-json \'{"depth":"raw"}\' vs '
        '\'{"depth":"excerpt"}\') under one synth model. '
        "Not comparable to reader runs.",
    )
    parser.add_argument(
        "--namespace-json",
        default="",
        help="namespace.register fields for each fresh item namespace, e.g. "
        '\'{"default_conflict_policy": "newest_wins"}\' (conflict-policy arms).',
    )
    parser.add_argument(
        "--ask-json",
        default="",
        help="JSON object merged into every memory.ask body (--answerer loom-ask), "
        'e.g. \'{"depth":"raw"}\' or \'{"max_answer_chars":600}\'.',
    )
    parser.add_argument(
        "--ask-arms",
        default="",
        help="JSON list of memory.ask overrides to answer EACH item with, on the "
        "same ingest and the same search hits (paired), e.g. "
        '\'[{"label":"excerpt","depth":"excerpt"},{"label":"raw","depth":"raw"}]\'. '
        "Implies --answerer loom-ask. One CSV row per (item, k, arm) with `arm` "
        "set; the summary prints judged accuracy per arm and the paired "
        "bootstrap delta of every arm against the first (the paired gate).",
    )
    parser.add_argument(
        "--top-k-sweep",
        default="",
        help="Comma-separated k values, e.g. '3,5,10,15,30'. Each item is "
        "ingested ONCE and answered at every k; the CSV gets one row per "
        "(item, k) with sweep_k set, and the summary prints accuracy and QA "
        "tokens per k — the accuracy-versus-tokens curve. "
        "Build columns ride the first k's row only.",
    )
    parser.add_argument(
        "--question-type",
        default="",
        help="Comma-separated question_type filter (e.g. "
        "single-session-assistant,knowledge-update). Empty = all. Applied "
        "before --shuffle/--limit so a category can be run complete.",
    )
    parser.add_argument(
        "--question-ids",
        default="",
        help="Comma-separated question_id filter, or @path to a file with one "
        "id per line. Empty = all. Applied with --question-type, before "
        "--shuffle/--limit, so a hand-picked set (the count questions, the "
        "misses of a prior run) can be replayed.",
    )
    parser.add_argument(
        "--relevance-gate",
        action="store_true",
        help="Paired arm: also run the reader on the SAME hits "
        "filtered by the server's relevance-gate prompt, and judge that answer "
        "too. Needs --answer-judge and --answerer reader. Adds gate_* columns "
        "and a second judge table; retrieval and ingest are shared, so the "
        "delta is the gate's.",
    )
    parser.add_argument(
        "--gate-kind",
        choices=["judge", "knee"],
        default="judge",
        help="Which gate the paired arm applies: 'judge' = the LLM relevance "
        "judge; 'knee' = the LLM-free similarity floor relative "
        "to the top hit (see --knee-floor).",
    )
    parser.add_argument(
        "--knee-floor",
        type=float,
        default=0.75,
        help="For --gate-kind knee: keep hits with cosine >= this fraction of "
        "the best hit's (floor of 3 rows).",
    )
    parser.add_argument(
        "--gate-model",
        default="",
        help="Model for the relevance-gate judge (default: --reader-model).",
    )
    parser.add_argument(
        "--lexical-scores",
        action="store_true",
        help="Also report token F1, BLEU-1 and ROUGE-L F1/recall of the SIMPLIFIED answer "
        "against the "
        "gold (bench/answer_scoring.py) — the columns published lexical tables report, "
        "next to (never instead of) the judge.",
    )
    parser.add_argument(
        "--stale-check",
        action="store_true",
        help="knowledge-update items only: for each WRONG judged answer, one more judge call "
        "asks whether the answer is the SUPERSEDED value from the earlier answer session — "
        "the 'hallucination of the past' rate, column stale_answer.",
    )
    parser.add_argument(
        "--reuse-csv",
        default="",
        help="an earlier --keep-namespaces run's CSV: each question it holds is answered "
        "again on that run's namespace with no ingest (its recall columns stay that run's)",
    )
    parser.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Do not erase this run's lme-* namespaces at the end (default erases them "
        "with one list-scoped, purging erasure).",
    )
    parser.add_argument(
        "--no-capture-consent",
        action="store_true",
        help="Do not register each bench namespace with capture_generation_io=true. "
        "Default registers it, so search_trace_log keeps the query text for replay "
        "(bench text is public dataset text; real consumers keep their own consent).",
    )
    parser.add_argument(
        "--simplify-model",
        default="",
        help="Model for the answer-simplification step. Default: "
        "the judge model when --answer-judge is set, else the model-free heuristic "
        "(labelled 'heuristic' in the CSV).",
    )
    add_gate_arguments(parser)  # --repeats / --gate* / --credit / --judge-calibration
    args = parser.parse_args()
    if args.reuse_csv:
        with open(args.reuse_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("namespace"):
                    _REUSE_NAMESPACES.setdefault(row["question_id"], row["namespace"])
        print(f"reusing {len(_REUSE_NAMESPACES)} namespaces from {args.reuse_csv}", file=sys.stderr)
    if args.relevance_gate and not args.answer_judge:
        parser.error("--relevance-gate needs --answer-judge")
    _search_json = json.loads(args.search_json) if args.search_json else None
    _ingest_json = json.loads(args.ingest_json) if args.ingest_json else None
    _ask_json = json.loads(args.ask_json) if args.ask_json else None
    sweep_ks = _parse_sweep(args.top_k_sweep)
    ask_arms = parse_ask_arms(args.ask_arms)
    if ask_arms and args.answerer != "loom-ask":
        print("NOTE: --ask-arms implies --answerer loom-ask", file=sys.stderr)
        args.answerer = "loom-ask"
    if _ask_json and args.answerer != "loom-ask":
        print("NOTE: --ask-json only applies with --answerer loom-ask", file=sys.stderr)
    if args.answerer == "loom-ask":
        print(
            "NOTE: --answerer loom-ask grades loom's own synthesis; the number is "
            "an A/B between memory.ask configurations, not comparable to the "
            "pinned-reader headline.",
            file=sys.stderr,
        )

    if args.search_mode == "oracle-routed":
        print(
            "WARNING: oracle-routed search-mode selects the retrieval mode "
            "from the dataset's GOLD question_type; production callers don't "
            "know the category — report this as an ablation, not a headline.",
            file=sys.stderr,
        )

    dataset_path = Path(args.dataset)
    try:
        dataset = await asyncio.to_thread(_load_dataset, dataset_path)
    except FileNotFoundError:
        print(f"missing dataset: {dataset_path}\nDownload from HF:", file=sys.stderr)
        print(
            "  curl -sL "
            "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
            f"resolve/main/{dataset_path.name} -o "
            f"{dataset_path}",
            file=sys.stderr,
        )
        return 2
    if args.question_type:
        wanted = {t.strip() for t in args.question_type.split(",") if t.strip()}
        dataset = [d for d in dataset if str(d.get("question_type", "")) in wanted]
    if args.question_ids:
        spec = str(args.question_ids)
        if spec.startswith("@"):
            raw = open(spec[1:], encoding="utf-8").read().split()
        else:
            raw = spec.split(",")
        wanted_ids = {t.strip() for t in raw if t.strip()}
        dataset = [d for d in dataset if str(d.get("question_id", "")) in wanted_ids]
    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(dataset)
    # Padding donors come from the whole filtered set, not only the --limit slice,
    # so a small run is padded as far as a full one.
    _donors = dataset
    if args.limit > 0:
        dataset = dataset[: args.limit]
    if args.evidence_position or abs(float(args.context_scale) - 1.0) > 1e-9:
        from bench.lme_variants import apply_variant, distractor_pool

        _pool = distractor_pool(_donors) if float(args.context_scale) > 1.0 else None
        _vrng = random.Random(args.seed)
        dataset = [
            apply_variant(
                d,
                position=args.evidence_position,
                scale=float(args.context_scale),
                pool=_pool,
                rng=_vrng,
            )
            for d in dataset
        ]
        print(
            f"variant: position={args.evidence_position or '-'} scale={args.context_scale:g} "
            f"applied to {len(dataset)} items",
            flush=True,
        )

    sem = asyncio.Semaphore(args.concurrency)
    reader_model: str = args.reader_model or _DEFAULT_READER_MODEL
    _simplify_model: str = args.simplify_model or (
        (args.judge_model or _DEFAULT_JUDGE_MODEL) if args.answer_judge else ""
    )
    judge_model: str | None = None
    judge_api_key = ""
    if args.answer_judge:
        judge_api_key = os.environ.get("OPENAI_API_KEY", "")
        if not judge_api_key:
            print("--answer-judge requires OPENAI_API_KEY", file=sys.stderr)
            return 2
        judge_model = args.judge_model or _DEFAULT_JUDGE_MODEL
        # Pin "gpt-4o" to the exact official LongMemEval judge id, matching
        # evaluate_qa.py's model_zoo.
        if judge_model == "gpt-4o":
            judge_model = _OFFICIAL_JUDGE_MODEL
        if judge_model != _OFFICIAL_JUDGE_MODEL:
            print(
                f"WARNING: judge_model={judge_model} is not the official "
                f"LongMemEval judge ({_OFFICIAL_JUDGE_MODEL}); judge-accuracy "
                "will not be comparable to published numbers or to "
                "official-judged baselines.",
                file=sys.stderr,
            )
        if reader_model != "gpt-4o-mini":
            print(
                f"NOTE: reader_model={reader_model} differs from the pinned "
                "baseline reader (gpt-4o-mini); judge-accuracy is a reader "
                "ablation, not comparable to baseline runs.",
                file=sys.stderr,
            )

    instrument = f", reader_model={reader_model}, judge_model={judge_model}" if judge_model else ""
    print(
        f"loom-longmemeval against {args.base_url}, items={len(dataset)}, "
        f"top_k={args.top_k}, concurrency={args.concurrency}, "
        f"search_mode={args.search_mode}, rerank={args.rerank}, "
        f"pool_unmatched={args.pool_unmatched}, reader={args.reader}"
        f"{instrument}"
    )

    async def _cleanup_post(path: str, body: dict) -> dict:
        async with httpx.AsyncClient() as c:
            return await _post(c, args.base_url + path, body, args.token)

    # Every namespace this run mints is erased when it exits, after a failure or
    # Ctrl-C too (one list-scoped, purging erasure); --keep-namespaces keeps them
    # for a later run. A refusal (writer token) is logged with the gc command.
    async with erase_on_exit(_cleanup_post, org="dev", keep=bool(args.keep_namespaces)):
        async with httpx.AsyncClient(timeout=120.0) as client:
            h = await client.get(args.base_url + "/v1/health", timeout=5.0)
            if h.status_code != 200:
                print(f"server unhealthy: {h.status_code}", file=sys.stderr)
                return 2

            results: list[ItemResult] = []
            failed_ids: list[str] = []

            async def runner(item: dict) -> None:
                try:
                    rows = await _run_one_item(
                        client,
                        args.base_url,
                        item,
                        args.top_k,
                        sem,
                        judge_model=judge_model,
                        reader_model=reader_model,
                        judge_api_key=judge_api_key,
                        token=args.token,
                        no_extraction_instructions=args.no_extraction_instructions,
                        consolidate=args.consolidate,
                        namespace_json=(
                            json.loads(args.namespace_json) if args.namespace_json else None
                        ),
                        sessions_per_call=max(1, int(args.sessions_per_call)),
                        search_mode=args.search_mode,
                        search_json=_search_json,
                        ingest_json=_ingest_json,
                        rerank=args.rerank,
                        pool_unmatched=args.pool_unmatched,
                        ingest_concurrency=args.ingest_concurrency,
                        reader=args.reader,
                        answerer=args.answerer,
                        ask_json=_ask_json,
                        sweep_ks=sweep_ks or None,
                        relevance_gate=bool(args.relevance_gate),
                        gate_model=args.gate_model,
                        ask_arms=ask_arms or None,
                        gate_kind=args.gate_kind,
                        knee_floor=float(args.knee_floor),
                        lexical_scores=bool(args.lexical_scores),
                        stale_check=bool(args.stale_check),
                        simplify_model=_simplify_model,
                        capture_consent=not bool(args.no_capture_consent),
                        keep_namespaces=bool(args.keep_namespaces),
                        repeats=int(args.repeats),
                        credit=bool(args.credit),
                    )
                    results.extend(rows)
                    # Print recall + judge outcome (if used) so progress lines
                    # show both signals.
                    for r in rows:
                        recall_marker = "✓" if r.recalled else "✗"
                        judge_marker = ""
                        if r.judge_label:
                            judge_marker = " J=✓" if r.judge_label == "CORRECT" else " J=✗"
                        if r.gate_judge_label:
                            judge_marker += (
                                f" G={'✓' if r.gate_judge_label == 'CORRECT' else '✗'}"
                                f"(-{r.gate_dropped})"
                            )
                        k_marker = f" k={r.sweep_k}" if r.sweep_k else ""
                        depth_marker = f" depth={r.ask_depth}" if r.ask_depth else ""
                        if r.arm:
                            depth_marker = f" [{r.arm}]{depth_marker}"
                        print(
                            f"  {recall_marker}{judge_marker}{k_marker}{depth_marker} "
                            f"{r.question_id} [{r.question_type}]",
                            flush=True,
                        )
                except (httpx.HTTPError, KeyError) as e:
                    qid = str(item.get("question_id", "?"))
                    print(f"  ! {qid} ERROR: {e}", file=sys.stderr, flush=True)
                    # An errored item stays in every denominator — scored as a
                    # recall-miss and (when judging) judge-WRONG. Dropping it
                    # silently shrinks N and inflates the headline.
                    failed_ids.append(qid)
                    results.append(
                        ItemResult(
                            question_id=qid,
                            question_type=str(item.get("question_type", "")),
                            question=str(item.get("question", "")),
                            gold_answer=str(item.get("answer", "")),
                            top_k_excerpts=[],
                            recalled=False,
                            answer_in_context=False,
                            judge_label="WRONG" if judge_model else None,
                            judge_rationale=f"item error: {e}",
                        )
                    )

            await asyncio.gather(*(runner(item) for item in dataset))

        # Aggregate by question_type — both recall and judge buckets. Under a
        # sweep the by-type table reads the primary k (--top-k when it is in the
        # sweep, else the first k); the per-k curve is printed separately.
        primary_k = 0
        if sweep_ks:
            primary_k = args.top_k if args.top_k in sweep_ks else sweep_ks[0]
        all_results = results
        results = [r for r in all_results if not sweep_ks or r.sweep_k == primary_k]
        if ask_arms:
            primary_arm = arm_label(ask_arms[0], 0)
            results = [r for r in results if r.arm == primary_arm]
        recall_by_type: dict[str, list[bool]] = defaultdict(list)
        aic_by_type: dict[str, list[bool]] = defaultdict(list)
        judge_by_type: dict[str, list[bool]] = defaultdict(list)
        for r in results:
            recall_by_type[r.question_type].append(r.recalled)
            aic_by_type[r.question_type].append(r.answer_in_context)
            if r.judge_label:
                judge_by_type[r.question_type].append(r.judge_label == "CORRECT")

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_write_csv, out_path, all_results)

        # Hypotheses JSONL for the official evaluate_qa.py cross-check:
        #   python bench/official/evaluate_qa.py gpt-4o <hyp> <dataset.json>
        hyp_path: Path | None = None
        if judge_model:
            hyp_path = Path(args.hyp_out) if args.hyp_out else out_path.with_suffix(".hyp.jsonl")
            with hyp_path.open("w") as hf:
                for r in results:
                    hf.write(
                        json.dumps({"question_id": r.question_id, "hypothesis": r.hypothesis})
                        + "\n"
                    )

        print()
        header_recall = "recall@K"
        header_judge = "judge-accuracy" if judge_model else "(no judge)"
        header_lex = "   F1  BLEU-1  RL-F1  RL-Rec" if args.lexical_scores else ""
        print(
            f"{'question_type':<28} {header_recall:>16} {'ans-in-ctx':>13} "
            f"{header_judge:>18}{header_lex}"
        )
        for qt in sorted(recall_by_type):
            rh = recall_by_type[qt]
            recall_rate = sum(rh) / len(rh) * 100.0 if rh else 0.0
            recall_cell = f"{sum(rh)}/{len(rh)} ({recall_rate:.1f}%)"
            ah = aic_by_type.get(qt, [])
            aic_rate = sum(ah) / len(ah) * 100.0 if ah else 0.0
            aic_cell = f"{sum(ah)}/{len(ah)} ({aic_rate:.0f}%)"
            if judge_model:
                jh = judge_by_type.get(qt, [])
                judge_rate = sum(jh) / len(jh) * 100.0 if jh else 0.0
                judge_cell = f"{sum(jh)}/{len(jh)} ({judge_rate:.1f}%)"
            else:
                judge_cell = "—"
            lex_cell = ""
            if args.lexical_scores:
                _rows_qt = [r for r in results if r.question_type == qt]
                _f1 = sum(r.f1 for r in _rows_qt) / len(_rows_qt) if _rows_qt else 0.0
                _b1 = sum(r.bleu1 for r in _rows_qt) / len(_rows_qt) if _rows_qt else 0.0
                _rl = sum(r.rouge_l_f1 for r in _rows_qt) / len(_rows_qt) if _rows_qt else 0.0
                _rr = sum(r.rouge_l_recall for r in _rows_qt) / len(_rows_qt) if _rows_qt else 0.0
                lex_cell = f" {_f1 * 100:5.1f} {_b1 * 100:6.1f} {_rl * 100:6.1f} {_rr * 100:6.1f}"
            print(f"{qt:<28} {recall_cell:>16} {aic_cell:>13} {judge_cell:>18}{lex_cell}")
        print()
        print(
            "DEPLOYMENT BY TYPE (search p50 · ask p25/p50/p75 · reader-context tokens · "
            "qa tok/item)"
        )
        print(
            f"{'question_type':<28} {'n':>4} {'judge':>14} {'search p50':>10} "
            f"{'ask p25':>8} {'ask p50':>8} {'ask p75':>8} {'ctx tok':>8} {'qa tok/item':>12}"
        )
        for row in deployment_by_type(results):
            print(
                f"{row['question_type']:<28} {row['n']:>4} {row['judge']:>14} "
                f"{row['search_p50_ms']:>10} {row['ask_p25_ms']:>8} {row['ask_p50_ms']:>8} "
                f"{row['ask_p75_ms']:>8} {row['ctx_tokens_mean']:>8} "
                f"{row['qa_tokens_per_item']:>12}"
            )
        if args.stale_check:
            ku_rows = [
                r for r in results if r.question_type == "knowledge-update" and r.judge_label
            ]
            ku_wrong = [r for r in ku_rows if r.judge_label == "WRONG"]
            ku_stale = [r for r in ku_wrong if r.stale_answer == "1"]
            ku_unknown = [r for r in ku_wrong if r.stale_answer == ""]
            print(
                f"knowledge-update stale answers: {len(ku_stale)} of {len(ku_wrong)} wrong "
                f"({len(ku_rows)} judged) state the superseded value"
                + (f"; {len(ku_unknown)} unchecked" if ku_unknown else "")
            )
        n = len(results)
        recalled = sum(1 for r in results if r.recalled)
        overall_recall = (recalled / n * 100.0) if n else 0.0
        print()
        print(f"OVERALL recall@{args.top_k}: {recalled} / {n}  ({overall_recall:.1f}%)")
        if judge_model:
            judge_correct = sum(1 for r in results if r.judge_label == "CORRECT")
            judge_total = sum(1 for r in results if r.judge_label)
            judge_overall = judge_correct / judge_total * 100.0 if judge_total else 0.0
            print(
                f"OVERALL answer-judge: {judge_correct} / {judge_total}  "
                f"({judge_overall:.1f}%)  "
                f"[reader={reader_model}; judge={judge_model}, "
                "official LongMemEval get_anscheck_prompt]"
            )
            ungraded = [r for r in results if str(r.judge_rationale).startswith("judge error")]
            if ungraded:
                print(
                    f"  {len(ungraded)} answer(s) counted WRONG because the judge call failed "
                    "after retries: " + ", ".join(r.question_id for r in ungraded)
                )
        if judge_model and args.relevance_gate:
            _gated = [r for r in results if r.gate_judge_label]
            _base_ok = sum(1 for r in _gated if r.judge_label == "CORRECT")
            _gate_ok = sum(1 for r in _gated if r.gate_judge_label == "CORRECT")
            _won = sum(
                1 for r in _gated if r.gate_judge_label == "CORRECT" and r.judge_label != "CORRECT"
            )
            _lost = sum(
                1 for r in _gated if r.gate_judge_label != "CORRECT" and r.judge_label == "CORRECT"
            )
            _aic_base = sum(1 for r in _gated if r.answer_in_context)
            _aic_gate = sum(1 for r in _gated if r.gate_answer_in_context)
            _dropped = sum(r.gate_dropped for r in _gated)
            _judged = sum(r.gate_kept + r.gate_dropped for r in _gated)
            _modes: dict[str, int] = defaultdict(int)
            for r in _gated:
                _modes[r.gate_mode] += 1
            print()
            print(
                f"RELEVANCE GATE [{args.gate_kind}] (paired, same hits): "
                f"judge {_base_ok} -> {_gate_ok} "
                f"/ {len(_gated)}  (+{_won} won, -{_lost} lost)  "
                f"ans-in-ctx {_aic_base} -> {_aic_gate}  "
                f"dropped {_dropped}/{_judged} rows "
                f"({(_dropped / _judged * 100.0) if _judged else 0.0:.0f}%)  "
                f"modes={dict(sorted(_modes.items()))}"
            )
            _gt: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
            for r in _gated:
                _gt[r.question_type].append(
                    (r.judge_label == "CORRECT", r.gate_judge_label == "CORRECT")
                )
            for qt in sorted(_gt):
                pairs = _gt[qt]
                print(
                    f"  {qt:<26} base {sum(b for b, _ in pairs)}/{len(pairs)}  "
                    f"gated {sum(g for _, g in pairs)}/{len(pairs)}"
                )
        n_failed = len(failed_ids)
        if n_failed:
            print(
                f"\nWARNING: n_failed={n_failed}/{n} items ERRORED; each is "
                f"counted as a recall-miss"
                f"{' and judge-WRONG' if judge_model else ''} in the totals "
                f"above (no silent denominator shrinkage): {', '.join(failed_ids)}",
                file=sys.stderr,
            )
        if ask_arms:
            print()
            print("ASK ARMS (paired on one ingest; delta = arm minus first arm, judge)")
            print(
                f"{'arm':<18} {'n':>4} {'judge':>14} {'deep fired':>11} {'ask p50 ms':>11} "
                f"{'qa tok/item':>12}  delta"
            )
            for row in arm_summary(all_results, seed=args.seed, gate_args=args):
                fr = row.get("flip_rate")
                print(
                    f"{row['arm']:<18} {row['n']:>4} {row['judge']:>14} {row['deep_fired']:>11} "
                    f"{row['ask_p50_ms']:>11} {row['qa_tokens_per_item']:>12}  {row['delta']}"
                    + (f"  flip {100 * fr:.1f}%" if fr is not None else "")
                )
                for line in row.get("gate_lines") or []:
                    print("    " + line)
        if sweep_ks:
            print()
            print(
                f"{'k':>4} {'recall@k':>14} {'judge':>14} {'qa tok/item':>12} "
                f"{'search p50 ms':>14} {'ask p50 ms':>11} {'deep fired':>11}"
            )
            for row in sweep_summary(all_results):
                print(
                    f"{row['k']:>4} {row['recall']:>14} {row['judge']:>14} "
                    f"{row['qa_tokens_per_item']:>12} {row['search_p50_ms']:>14} "
                    f"{row['ask_p50_ms']:>11} {row['deep_fired']:>11}"
                )
        eff = efficiency_summary(all_results)
        print()
        print(
            "EFFICIENCY  build: "
            f"{eff['build_tokens_k']}k tokens / {eff['build_calls']} calls / "
            f"{eff['memories_written']} memories "
            f"({eff['tokens_per_memory']} prompt tokens per memory) / "
            f"{eff['build_seconds']}s   qa: {eff['qa_tokens_k']}k tokens / "
            f"{eff['qa_calls']} calls / "
            f"{eff['qa_seconds']}s   total {eff['total_tokens_k']}k tokens"
        )
        print(
            f"  per item: build {eff['per_item']['build_tokens_k']}k tok, "
            f"{eff['per_item']['build_seconds']}s; "
            f"qa {eff['per_item']['qa_tokens_k']}k tok, {eff['per_item']['qa_seconds']}s"
        )
        print(f"\nwrote {out_path}")
        if hyp_path is not None:
            print(
                f"wrote {hyp_path}  (cross-check: python bench/official/evaluate_qa.py "
                f"gpt-4o {hyp_path} {dataset_path})"
            )
    if n and n_failed == n:
        # A 100%-error run produces an artifact that looks like a result but
        # measures nothing; refuse it so it can't be mistaken for a baseline.
        print(
            "REFUSING RESULT: every item errored — fix the stack and re-run.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
