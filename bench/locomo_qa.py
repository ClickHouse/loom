"""LoCoMo question-answering accuracy against Loom, per question category.

LoCoMo is a widely reported conversational-memory benchmark: ten long two-person
conversations (~600 turns, ~26k tokens each), ~200 questions per conversation
in five categories. The system ingests the conversation into memory, then a
READER answers each question from retrieved memories only (no transcript), and
a JUDGE grades the answer CORRECT/WRONG against the gold answer.

This file exists so loom has a number in the same table.

Pipeline per conversation (one namespace each, so conversations never leak):
  1. One memory.set_from_messages per SESSION, in session-date order (the
     default; it measured +5.3 multi-hop over 4-way concurrent ingest),
     `observation_date` = the session's date so relative time
     references resolve; both speakers are
     humans, so every turn is a `user` message prefixed with the speaker name.
     Image turns carry the dataset's BLIP caption as text (no pixels:
     extracted text, not the asset, is what memory needs).
  2. Per question: memory.search (default `rrf`, the server default) →
     reader → judge. `--reader ask` routes through memory.ask synthesis
     instead of the external reader, which scores loom's own answer path.

Categories (the dataset's integer labels, as the published evaluation
harnesses map them):
  1 multi_hop · 2 temporal · 3 open_domain · 4 single_hop · 5 adversarial

The published harnesses EXCLUDE category 5. Loom scores it: the gold behaviour is to
abstain, and an answer that asserts anything is WRONG. Two overalls are
printed — `overall_comparable` (categories 1–4, the published column) and
`overall_all` (1–5).

Reader and judge are pinned independently of LOOM_LLM_MODEL so the number is
about the memory system, not about whichever model the server happens to run.
Defaults match a common published setup: reader gpt-4.1-mini, judge gpt-4.1. Changing
either makes the number non-comparable; the CSV records both.

Cost (full set, 10 conversations, 1,986 questions): ~190 extraction calls on
the server's LLM, ~2k reader + ~2k judge calls. Expect ~$3–6 and ~40 min with
the default concurrency. `--limit-conversations 1` is a ~$0.50 smoke run.

Run:
    LOOM_API_KEY=... python bench/locomo_qa.py --dataset bench/datasets/locomo10.json \\
        --base-url http://localhost:7777 --out bench/locomo_qa.csv
    # re-score existing namespaces without re-ingesting (reader/judge variance):
    python bench/locomo_qa.py ... --namespaces-json bench/locomo_qa.namespaces.json

The dataset is third-party (snap-research/locomo, `data/locomo10.json`) and is
not vendored; `bench/datasets/` is gitignored.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import re
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bench.judges import achat, is_claude

try:
    from bench._gate import (
        add_gate_arguments,
        flip_rate,
        gate_from_args,
        group_by,
        identical_share,
        labels_by_key,
        measurement_fields,
    )
    from bench._gate import majority_label as _gate_majority_label
    from bench.answer_scoring import (
        bleu1,
        cohen_kappa,
        label_agreement,
        simplify_heuristic,
        token_f1,
    )
    from bench.bench_namespaces import (
        erase_bench_namespaces,
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from _gate import (  # type: ignore[no-redef]
        add_gate_arguments,
        flip_rate,
        gate_from_args,
        group_by,
        identical_share,
        labels_by_key,
        measurement_fields,
    )
    from _gate import majority_label as _gate_majority_label  # type: ignore[no-redef]
    from answer_scoring import (  # type: ignore[no-redef]
        bleu1,
        cohen_kappa,
        label_agreement,
        simplify_heuristic,
        token_f1,
    )
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_bench_namespaces,
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

CATEGORY_NAMES: dict[int, str] = {
    1: "multi_hop",
    2: "temporal",
    3: "open_domain",
    4: "single_hop",
    5: "adversarial",
}
COMPARABLE_CATEGORIES: frozenset[int] = frozenset({1, 2, 3, 4})

_DEFAULT_READER_MODEL = os.environ.get("BENCH_READER_MODEL", "gpt-4.1-mini")
_DEFAULT_JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "gpt-4.1")
_OPENAI_CHAT_URL = os.environ.get(
    "BENCH_OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions"
)

# "1:56 pm on 8 May, 2023" → 2023-05-08
_SESSION_DATE_RE = re.compile(
    r"(?:(\d{1,2}):(\d{2})\s*(am|pm)\s+on\s+)?(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}

_ABSTAIN_TEXT = "Not enough information in memory to answer."


def session_date_to_iso(raw: str) -> str:
    """LoCoMo session stamps look like '1:56 pm on 8 May, 2023'. Return the
    ISO date the extractor's observation_date consumes; '' when unparseable."""
    m = _SESSION_DATE_RE.search(raw or "")
    if not m:
        return ""
    day, month_name, year = int(m.group(4)), m.group(5).lower(), int(m.group(6))
    month = _MONTHS.get(month_name) or _MONTHS.get(
        next((k for k in _MONTHS if k.startswith(month_name[:3])), ""), 0
    )
    if not month:
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def iter_sessions(conversation: dict[str, Any]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Yield (session_key, iso_date, turns) in session order. Session keys are
    `session_N` with a sibling `session_N_date_time`; numbering can have gaps."""
    keys = sorted(
        (k for k in conversation if re.fullmatch(r"session_\d+", k)),
        key=lambda k: int(k.split("_")[1]),
    )
    out: list[tuple[str, str, list[dict[str, Any]]]] = []
    for k in keys:
        turns = conversation.get(k) or []
        if not isinstance(turns, list) or not turns:
            continue
        out.append((k, session_date_to_iso(str(conversation.get(f"{k}_date_time", ""))), turns))
    return out


def turn_to_message(turn: dict[str, Any]) -> dict[str, str]:
    """Both LoCoMo speakers are humans, so the role is `user` and the speaker
    rides in the content. An image turn contributes its caption as text."""
    speaker = str(turn.get("speaker") or "Speaker").strip()
    text = str(turn.get("text") or "").strip()
    caption = str(turn.get("blip_caption") or "").strip()
    if caption:
        text = f"{text} [shared a photo: {caption}]".strip()
    # `name` carries the speaker to the extractor; the
    # content keeps the prefix too so older servers see the same text.
    return {"role": "user", "content": f"{speaker}: {text}", "name": speaker}


_EVIDENCE_ID_RE = re.compile(r"^D(\d+):")


def evidence_session_count(q: dict[str, Any]) -> int:
    """How many distinct sessions a question's gold evidence cites."""
    sessions: set[int] = set()
    for ev in q.get("evidence") or []:
        m = _EVIDENCE_ID_RE.match(str(ev))
        if m:
            sessions.add(int(m.group(1)))
    return len(sessions)


def evidence_distance_bucket(n: int) -> str:
    return "1" if n <= 1 else "2" if n == 2 else "3+"


def evidence_sessions_cited(q: dict[str, Any]) -> set[int]:
    """The distinct session numbers a question's gold evidence cites."""
    out: set[int] = set()
    for ev in q.get("evidence") or []:
        m = _EVIDENCE_ID_RE.match(str(ev))
        if m:
            out.add(int(m.group(1)))
    return out


_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def turn_speakers(conversation: dict[str, Any]) -> dict[str, str]:
    """`dia_id` → speaker for every turn of a LoCoMo conversation."""
    out: dict[str, str] = {}
    for _, _, turns in iter_sessions(conversation):
        for t in turns:
            dia = str(t.get("dia_id") or "").strip()
            if dia:
                out[dia] = str(t.get("speaker") or "").strip()
    return out


def speaker_named(question: str, speakers: tuple[str, ...]) -> str:
    """The one conversation speaker the question names; "" when none or both."""
    found = []
    for sp in speakers:
        if not sp:
            continue
        pat = _WORD_RE_CACHE.get(sp)
        if pat is None:
            pat = _WORD_RE_CACHE[sp] = re.compile(r"\b" + re.escape(sp) + r"\b", re.IGNORECASE)
        if pat.search(question):
            found.append(sp)
    return found[0] if len(found) == 1 else ""


def evidence_speaker(q: dict[str, Any], conversation: dict[str, Any] | None) -> str:
    """Who said the gold evidence, relative to the speaker the question names.
    Deterministic, read off the transcript, no LLM.

    `named`: every evidence turn is by the speaker the question names.
    `other`: none is. "" when the check cannot be made (no evidence ids, both
    or neither speaker named, no conversation).

    LoCoMo's adversarial items come in two kinds this separates. A speaker
    swap (`other`, 335 of 446) names the partner of the person who said it,
    and the right answer is "<named> never said that" — the attribution test.
    A content swap (`named`, 107) keeps the speaker and changes the fact
    (cake for ice cream, painting for game), and the right answer is "the
    memories do not record that" — the premise test. Loom's misses live
    almost entirely in the first kind, so the bench reports them apart."""
    if not conversation:
        return ""
    speakers = (
        str(conversation.get("speaker_a") or ""),
        str(conversation.get("speaker_b") or ""),
    )
    named = speaker_named(str(q.get("question") or ""), speakers)
    if not named:
        return ""
    by_turn = turn_speakers(conversation)
    ev = [by_turn.get(str(e).strip(), "") for e in (q.get("evidence") or [])]
    ev = [e for e in ev if e]
    if not ev:
        return ""
    if all(e == named for e in ev):
        return "named"
    if all(e != named for e in ev):
        return "other"
    return ""


def plane_of(key: str) -> str:
    """The plane a hit key came from: `digest`, `chunk`, `period` or `fact`."""
    k = key.strip()
    if k.startswith("summary:"):
        return "digest"
    if k.startswith("chunk:"):
        return "chunk"
    if k.startswith("period_digest_"):
        return "period"
    return "fact"


def plane_mix(top_keys_rows: Any) -> dict[str, float]:
    """Share of each plane over every hit key in the given `top_keys` strings
    (`" | "`-joined memory keys, as the harness records them). Empty when no
    hits were recorded."""
    counts: dict[str, int] = {}
    total = 0
    for row in top_keys_rows:
        for key in str(row or "").split("|"):
            if not key.strip():
                continue
            counts[plane_of(key)] = counts.get(plane_of(key), 0) + 1
            total += 1
    if not total:
        return {}
    return {p: round(counts[p] / total, 3) for p in sorted(counts)}


def evidence_gap(q: dict[str, Any], last_session: int) -> int:
    """Sessions between the conversation's final session and the EARLIEST gold evidence
    — the "evidence distance gap" (bins 1–5 … 26–31). LoCoMo asks every question after
    the whole conversation, so the query's session is the last one. 0 when the question
    cites no evidence (adversarial)."""
    cited = evidence_sessions_cited(q)
    if not cited or last_session <= 0:
        return 0
    return max(0, last_session - min(cited))


GAP_BINS: tuple[tuple[int, int], ...] = ((1, 5), (6, 10), (11, 15), (16, 20), (21, 25), (26, 99))


def evidence_gap_bucket(gap: int) -> str:
    if gap <= 0:
        return "0"
    for lo, hi in GAP_BINS:
        if lo <= gap <= hi:
            return f"{lo}-{hi}" if hi < 99 else f"{lo}+"
    return "26+"


_SESSION_NUM_RE = re.compile(r"session_(\d+)$")


def _session_number(session_id: str) -> int | None:
    """`<namespace>:session_N` (the id the ingest wrote) → N."""
    m = _SESSION_NUM_RE.search(str(session_id or ""))
    return int(m.group(1)) if m else None


def hit_session_numbers(hits: list[dict[str, Any]], namespace: str) -> list[int]:
    """Session numbers of the hits, in rank order, from the `session_id` a
    hit carries (`<namespace>:session_N`, the id the ingest wrote). Hits
    without one (older servers, summary/chunk/derived rows) contribute
    nothing; the caller reports coverage so a missing field is visible."""
    out: list[int] = []
    for h in hits:
        sid = str(h.get("session_id") or "")
        m = re.search(r"session_(\d+)$", sid)
        if m:
            out.append(int(m.group(1)))
            continue
        # A period digest has no session of its own; it carries
        # the sessions it compressed as `src:summary:<session_id>` tags, and a
        # hit on it covers every one of them.
        for tag in h.get("tags") or []:
            t = str(tag)
            if t.startswith("src:summary:"):
                m = re.search(r"session_(\d+)$", t)
                if m:
                    out.append(int(m.group(1)))
    return out


def source_recall_at(hits_sessions: list[int], gold: set[int], k: int) -> float | None:
    """Recall@k over source sessions: share of the gold evidence sessions
    that appear among the first k hits' sessions (source-id group recall, at
    session granularity). None when the question cites
    no evidence."""
    if not gold:
        return None
    seen = set(hits_sessions[:k])
    return len(gold & seen) / len(gold)


def conv_asof(conversation: dict[str, Any] | None) -> str | None:
    """ISO datetime of the conversation's LAST session — the "now" the LoCoMo
    questions are authored against (e.g. 'how long ago', 'last summer' resolve
    against this, not wall-clock today). Returns None if unparseable. A client
    querying historical data should pass this as memory.ask `as_of`; without it
    loom anchors temporal arithmetic to today (datetime.now), which is correct
    for a live agent but wrong for a fixed-in-the-past benchmark corpus."""
    from datetime import datetime as _dt

    if not conversation:
        return None
    best = None
    for k, v in conversation.items():
        if not k.endswith("_date_time") or not v:
            continue
        try:
            d = _dt.strptime(str(v).strip(), "%I:%M %p on %d %B, %Y")
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    return best.isoformat() if best else None


def gold_answer(q: dict[str, Any]) -> str:
    if int(q.get("category", 0)) == 5:
        # The dataset's `adversarial_answer` is the distractor the question
        # presupposes; the correct behaviour is to say the memory has no answer.
        return ""
    a = q.get("answer")
    return str(a).strip() if a is not None else ""


# ---------------------------------------------------------------- prompts

_READER_TEMPLATE = (
    "You are answering a question about two people from memories extracted "
    "from their past conversations. Use ONLY the memories below. Each memory "
    "carries the date it is about when known.\n\n"
    "If the memories do not contain the answer, reply exactly: "
    f"{_ABSTAIN_TEXT}\n"
    "Otherwise answer in one short sentence, keeping names, dates and "
    "quantities exact.\n\n"
    "Memories:\n{history}\n\n"
    "Question: {question}\nAnswer:"
)

# Adapted from the widely used LoCoMo judge prompt: generous
# on phrasing and date format, strict on topic. Returns one label.
_JUDGE_TEMPLATE = (
    "Your task is to label an answer to a question as CORRECT or WRONG. You "
    "will be given:\n"
    "  (1) a question posed about two people's past conversations,\n"
    "  (2) a gold (ground truth) answer,\n"
    "  (3) a generated answer.\n\n"
    "The gold answer is usually short and names the referenced topic, e.g.\n"
    "  Question: Do you remember what I got the last time I went to Hawaii?\n"
    "  Gold answer: A shell necklace\n"
    "The generated answer may be longer; be generous — if it touches the same "
    "topic and does not contradict the gold answer, label it CORRECT.\n\n"
    "For time questions the gold answer is a date, month or year. Be generous "
    "about format ('May 7th' vs '7 May') and about relative phrasing that "
    "resolves to the same period, but a different date is WRONG.\n\n"
    "An answer that says the information is not available, when the gold "
    "answer exists, is WRONG.\n\n"
    "Question: {question}\nGold answer: {gold}\nGenerated answer: {generated}\n\n"
    'Reply with a JSON object with a single key "label" whose value is '
    "CORRECT or WRONG. Do not include both words."
)

# Category 5: the question presupposes something the conversation never said.
_JUDGE_ADVERSARIAL_TEMPLATE = (
    "A question was asked about two people's past conversations, but the "
    "conversations never contained the answer — the question presupposes "
    "something that was not said. The correct behaviour is to say the "
    "information is not available.\n\n"
    "Label the generated answer CORRECT if it declines, abstains, or says the "
    "memories/conversations do not contain this. Label it WRONG if it asserts "
    "a substantive answer as fact.\n\n"
    "Question: {question}\nGenerated answer: {generated}\n\n"
    'Reply with a JSON object with a single key "label" whose value is '
    "CORRECT or WRONG. Do not include both words."
)


def parse_judge_label(reply: str) -> str | None:
    """Accept `{"label": "CORRECT"}` or a bare word; None when ambiguous."""
    text = (reply or "").strip()
    try:
        obj = json.loads(text[text.find("{") : text.rfind("}") + 1])
        label = str(obj.get("label", "")).strip().upper()
        if label in {"CORRECT", "WRONG"}:
            return label
    except (ValueError, AttributeError):
        pass
    # Whole words: "INCORRECT" contains "CORRECT" and means WRONG.
    up = text.upper()
    has_c = re.search(r"\bCORRECT\b", up) is not None
    has_w = re.search(r"\b(WRONG|INCORRECT)\b", up) is not None
    if has_c and not has_w:
        return "CORRECT"
    if has_w and not has_c:
        return "WRONG"
    return None


def format_history(hits: list[dict[str, Any]]) -> str:
    """One line per hit. The EVENT date (`temporal_anchor`, resolved by the
    extractor from the turn) leads; the SESSION date (`valid_at`, when it was
    said) rides along only when it differs: shown the session date alone, a
    reader answers "when did X happen?" with the date X was *discussed*."""
    lines = []
    for h in hits:
        event = str(h.get("temporal_anchor") or "")[:10]
        said = str(h.get("valid_at") or "")[:10]
        if event and said and event != said:
            prefix = f"[{event}, said on {said}] "
        elif event or said:
            prefix = f"[{event or said}] "
        else:
            prefix = ""
        lines.append(f"- {prefix}{h.get('content_excerpt', '')}")
    return "\n".join(lines) if lines else "(no memories retrieved)"


# ---------------------------------------------------------------- results


@dataclass
class QuestionResult:
    sample_id: str
    namespace: str
    question_index: int
    category: int
    category_name: str
    question: str
    gold: str
    generated: str
    label: str  # CORRECT | WRONG | ERROR
    judge_reply: str
    abstained: bool
    hits: int
    search_ms: int
    embedding_model: str
    search_mode: str
    reader: str
    reader_model: str
    judge_model: str
    top_keys: str
    # Distinct sessions the gold evidence spans (LoCoMo `evidence` ids
    # `D<session>:<turn>`); 0 when the item has none (adversarial). The
    # dependency distance between where a fact was said and where it is
    # asked is an axis worth reporting, and accuracy falls along it (from
    # one evidence session to three or more).
    evidence_sessions: int = 0
    # Evidence distance gap: sessions between the conversation's last session
    # and the earliest gold evidence. Flat dense retrieval is expected to fall
    # off along this axis, not along the span count above.
    evidence_gap: int = 0
    # Source recall: share of gold evidence sessions whose memories appear in
    # the first 5 / 10 hits (needs hits that carry `session_id`; -1 when the
    # server returned none, so a missing field never reads as zero recall).
    source_recall_5: float = -1.0
    source_recall_10: float = -1.0
    hits_with_session: int = 0
    # Repeats (--repeats N): every judged label this question received, in
    # run order, and the share that agreed with the majority written to
    # `label`. Two identical runs of this bench on 164 questions differed by
    # up to 4.9 points (11 vs 3 discordant pairs) with the reader at
    # temperature 0; the per-question majority is how an arm delta becomes
    # readable against that band.
    repeat_labels: str = ""
    repeat_agreement: float = 1.0
    # Share of the repeats whose `top_keys` equalled the first
    # run's — the retrieval pin. memory.search is deterministic (0/24 changed
    # over 5 calls); memory.ask re-decomposes inside synthesis (43% of top-key
    # lists differed between two runs), so a flip on an identical window is
    # the reader's and one on a changed window is retrieval's.
    repeat_window_identical: float = 1.0
    # --reader ask only: Jaccard of the sessions the answer
    # cited against the gold evidence sessions; the answer's mean window
    # score of its cited rows (-9 = not measured / no window). -1 = not
    # measured.
    cited_jaccard: float = -1.0
    temporal_consistency: float = -9.0
    # Write-policy arm the conversation was ingested under:
    # "default" or a namespace conflict policy such as newest_wins. A memory
    # operation can be scored by whether the answer comes out right; this
    # column is that signal, one arm per namespace.
    policy: str = "default"
    # Who said the gold evidence relative to the speaker the question names
    # (`evidence_speaker`): "named", "other" or "" when unclassifiable. For
    # adversarial items this is the kind — content swap vs speaker swap.
    evidence_speaker: str = ""
    # The published LoCoMo lexical columns: token F1 and BLEU-1 against
    # the gold string, on the answer as written and after the model-free
    # simplification (`answer_scoring.simplify_heuristic`); -1 when the
    # question has no gold (adversarial) or the row errored. `context_tokens`
    # is memory.ask's own estimate of the prompt it read (an efficiency
    # column).
    f1: float = -1.0
    bleu1: float = -1.0
    f1_simplified: float = -1.0
    bleu1_simplified: float = -1.0
    context_tokens: int = -1
    # Type-aware temporal scoring (bench/answer_scoring):
    # the gold's kind (date / year / duration / other) and, where the kind
    # applies, unit-aware date equality and ε-exact duration match of the
    # generated answer ("1" / "0" / "" not applicable). Beside the judge, never
    # instead of it: a WRONG with date_match "1" is a strict judge or extra
    # wrong text; a CORRECT with date_match "0" is a lenient judge.
    temporal_kind: str = ""
    date_match: str = ""
    duration_match: str = ""
    # Reader wall time (ms) for the external reader — the model call that turns
    # hits into an answer. 0 with --reader ask, where `search_ms` already
    # spans the whole memory.ask call (retrieval + synthesis). `search_ms +
    # answer_ms` is the per-question "total" latency, reported beside search.
    # The judge is not in it.
    answer_ms: int = 0
    # --second-judge-model: the label a second, independent judge model gave
    # the same generated answer on the sampled questions ("" = not sampled).
    # Agreement and Cohen's kappa over the sample land in the summary, so an
    # accuracy is printed next to how much of it the judge can move.
    second_judge_label: str = ""


@dataclass
class ConversationBuild:
    sample_id: str
    namespace: str
    sessions: int = 0
    turns: int = 0
    written: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    llm_model: str = ""
    errors: list[str] = field(default_factory=list)


def majority_label(labels: list[str]) -> str:
    """The label a question keeps after N repeats: CORRECT only when more
    than half the judged runs said so (a tie is WRONG — the conservative
    side); ERROR only when every run errored. Judged runs outvote errors."""
    return _gate_majority_label(list(labels))


def fold_repeats(runs: list[QuestionResult]) -> QuestionResult:
    """One row from N runs of the same question: the first run's fields,
    `label` replaced by the majority, the run labels and their agreement
    recorded so a flip-prone question is visible in the CSV."""
    labels = [r.label for r in runs]
    base = runs[0]
    base.label = majority_label(labels)
    base.repeat_labels = "|".join(labels)
    agree = sum(lb == base.label for lb in labels)
    base.repeat_agreement = round(agree / len(labels), 3) if labels else 1.0
    base.repeat_window_identical = identical_share([r.top_keys for r in runs])
    return base


def percentile_ms(values: list[int], q: float) -> int | None:
    """Nearest-rank percentile of a latency sample (q in 0–100); None on empty."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(q / 100.0 * len(ordered)) - 1))
    return int(ordered[idx])


def run_accuracy_spread(results: list[QuestionResult], cats: set[int]) -> dict[str, Any]:
    """Accuracy per repeated run and its mean ± sd, over `cats` (LoCoMo is commonly
    reported as the mean and standard deviation of several judge runs); `--repeats N`
    here re-answers AND re-judges, so run i's accuracy is read from the i-th label in
    `repeat_labels`. Empty (None fields) when the run was not repeated."""
    rows = [r for r in results if r.category in cats and "|" in (r.repeat_labels or "")]
    if not rows:
        return {"runs": 0, "accuracies": [], "mean": None, "sd": None}
    n_runs = min(len(r.repeat_labels.split("|")) for r in rows)
    accs: list[float] = []
    for i in range(n_runs):
        judged = [r.repeat_labels.split("|")[i] for r in rows]
        judged = [lb for lb in judged if lb in ("CORRECT", "WRONG")]
        if judged:
            accs.append(round(100.0 * sum(lb == "CORRECT" for lb in judged) / len(judged), 2))
    return {
        "runs": n_runs,
        "accuracies": accs,
        "mean": round(statistics.mean(accs), 2) if accs else None,
        "sd": round(statistics.stdev(accs), 2) if len(accs) > 1 else (0.0 if accs else None),
    }


def footprint_from_health(resp: dict[str, Any]) -> dict[str, Any]:
    """Memory footprint of one namespace from a memory.health response: live
    (active) rows and the server's stored-value token estimate (chars/4,
    `footprint.value_tokens_est`) — the stored footprint per LoCoMo
    conversation (the raw transcript is about 26k tokens). `tokens_est` is None on a
    server whose health report predates the footprint section."""
    fp = resp.get("footprint") or {}
    tokens = fp.get("value_tokens_est")
    return {
        "memories": int(resp.get("live") or 0),
        "tokens_est": None if tokens is None else int(tokens),
    }


async def namespace_footprint(
    client: httpx.AsyncClient, base_url: str, token: str, identity: dict[str, str]
) -> dict[str, Any]:
    """One memory.health call per namespace (it aggregates across agents,
    users and sessions; memory.list is scoped to one identity tuple and would
    see only the rows written under the bench's own session id). Errors (a
    namespace already erased, an older server) return zeros with `error` = 1
    rather than failing the run: the footprint is a report column, not the
    result."""
    try:
        resp = await _post(
            client,
            base_url + "/v1/memory.health",
            {"org": identity["org"], "namespace": identity["namespace"], "agent": "-", "top_k": 1},
            token,
            retries=2,
        )
    except (httpx.HTTPError, ValueError, KeyError):
        return {"memories": 0, "tokens_est": None, "error": 1}
    return {**footprint_from_health(resp), "error": 0}


def second_judge_sample(results: list[QuestionResult], n: int) -> list[QuestionResult]:
    """An evenly spaced, deterministic sample of the judged (non-ERROR) results,
    ordered by conversation then question, so two runs re-judge the same rows."""
    judged = sorted(
        (r for r in results if r.label in ("CORRECT", "WRONG")),
        key=lambda r: (r.sample_id, r.question_index),
    )
    if n <= 0 or not judged:
        return []
    if n >= len(judged):
        return judged
    step = len(judged) / n
    return [judged[int(i * step)] for i in range(n)]


def judge_agreement_summary(results: list[QuestionResult], second_model: str) -> dict[str, Any]:
    """Agreement and Cohen's kappa between --judge-model and the second judge
    over the rows the second judge graded; the disagreements by category."""
    rows = [r for r in results if r.second_judge_label in ("CORRECT", "WRONG")]
    a = [r.label for r in rows]
    b = [r.second_judge_label for r in rows]
    by_cat: dict[str, int] = {}
    for r in rows:
        if r.label != r.second_judge_label:
            by_cat[r.category_name] = by_cat.get(r.category_name, 0) + 1
    return {
        "second_judge_model": second_model,
        "sampled": len(rows),
        "agreement": round(label_agreement(a, b), 3) if rows else None,
        "kappa": cohen_kappa(a, b) if rows else None,
        "disagreements": len(rows) - int(round(label_agreement(a, b) * len(rows))) if rows else 0,
        "disagreements_by_category": by_cat,
        # The judge's own noise: the accuracy the second judge would report.
        "second_judge_accuracy": (
            round(100.0 * sum(1 for x in b if x == "CORRECT") / len(b), 2) if b else None
        ),
        "first_judge_accuracy_on_sample": (
            round(100.0 * sum(1 for x in a if x == "CORRECT") / len(a), 2) if a else None
        ),
    }


def aggregate(results: list[QuestionResult]) -> dict[str, Any]:
    """Per-category accuracy over judged questions (ERROR rows are excluded
    from the denominator and counted separately)."""
    by_cat: dict[str, dict[str, Any]] = {}
    for r in results:
        b = by_cat.setdefault(r.category_name, {"n": 0, "correct": 0, "errors": 0, "abstained": 0})
        if r.label == "ERROR":
            b["errors"] += 1
            continue
        b["n"] += 1
        b["correct"] += r.label == "CORRECT"
        b["abstained"] += r.abstained
    for b in by_cat.values():
        b["accuracy"] = round(100.0 * b["correct"] / b["n"], 2) if b["n"] else None
        b["abstain_rate"] = round(100.0 * b["abstained"] / b["n"], 2) if b["n"] else None
    # Which plane filled the top-k, per category: session digests (`summary:`
    # keys), verbatim chunks (`chunk:`), period digests (`period_digest_`) and
    # facts (everything else) — the memory type that served each question
    # category across loom's fused legs, so a plane's share can be set against the
    # category it serves before a planner is taught to name it.
    for name, b in by_cat.items():
        b["plane_mix"] = plane_mix(
            r.top_keys for r in results if r.category_name == name and r.label != "ERROR"
        )
    # The published lexical columns: token F1 / BLEU-1 ×100 per category and two overall
    # averages — over questions and over categories — plus the mean prompt size
    # memory.ask reported.
    for name, b in by_cat.items():
        rows = [r for r in results if r.category_name == name and r.f1 >= 0]
        b["f1"] = round(100.0 * sum(r.f1 for r in rows) / len(rows), 2) if rows else None
        b["bleu1"] = round(100.0 * sum(r.bleu1 for r in rows) / len(rows), 2) if rows else None
        b["f1_simplified"] = (
            round(100.0 * sum(r.f1_simplified for r in rows) / len(rows), 2) if rows else None
        )
        b["bleu1_simplified"] = (
            round(100.0 * sum(r.bleu1_simplified for r in rows) / len(rows), 2) if rows else None
        )
        toks = [r.context_tokens for r in rows if r.context_tokens >= 0]
        b["context_tokens_mean"] = round(sum(toks) / len(toks)) if toks else None

    # Accuracy exceeding recall: questions answered correctly although none of
    # the gold evidence sessions reached the top 10. A high share means recall
    # against annotated evidence under-reads retrieval; reported with the
    # plain recall@5 / @10 means.
    judged_recall = [r for r in results if r.label != "ERROR" and r.source_recall_10 >= 0]
    zero10 = [r for r in judged_recall if r.source_recall_10 == 0.0]
    zero5 = [r for r in judged_recall if r.source_recall_5 >= 0 and r.source_recall_5 == 0.0]
    recall_info = {
        "recall_at_5": (
            round(
                100.0
                * sum(r.source_recall_5 for r in judged_recall if r.source_recall_5 >= 0)
                / max(1, sum(1 for r in judged_recall if r.source_recall_5 >= 0)),
                2,
            )
            if judged_recall
            else None
        ),
        "recall_at_10": (
            round(100.0 * sum(r.source_recall_10 for r in judged_recall) / len(judged_recall), 2)
            if judged_recall
            else None
        ),
        "zero_recall_at_5": len(zero5),
        "zero_recall_at_5_correct": sum(r.label == "CORRECT" for r in zero5),
        "zero_recall_at_10": len(zero10),
        "zero_recall_at_10_correct": sum(r.label == "CORRECT" for r in zero10),
    }

    # Repeats: questions whose runs disagreed, and the mean agreement — the
    # bench's own noise reading, printed next to the accuracy it qualifies.
    repeated = [r for r in results if "|" in (r.repeat_labels or "")]
    unstable = [
        r for r in repeated if len(set(r.repeat_labels.split("|")) & {"CORRECT", "WRONG"}) > 1
    ]
    repeats_info = {
        "questions_repeated": len(repeated),
        "unstable": len(unstable),
        "unstable_rate": round(100.0 * len(unstable) / len(repeated), 2) if repeated else None,
        "agreement_mean": (
            round(sum(r.repeat_agreement for r in repeated) / len(repeated), 3)
            if repeated
            else None
        ),
        # The per-item flip rate the gate prints beside the
        # delta, and the retrieval pin — how many repeated items saw the same
        # top-k window on every run (flips there are the reader's alone).
        "flip_rate": flip_rate(r.repeat_labels.split("|") for r in repeated),
        "window_identical_rate": (
            round(sum(r.repeat_window_identical >= 1.0 for r in repeated) / len(repeated), 4)
            if repeated
            else None
        ),
        "flip_rate_identical_window": flip_rate(
            r.repeat_labels.split("|") for r in repeated if r.repeat_window_identical >= 1.0
        ),
        "flip_rate_changed_window": flip_rate(
            r.repeat_labels.split("|") for r in repeated if r.repeat_window_identical < 1.0
        ),
        # Per-run accuracy and its spread ("mean ± sd over runs"), over the comparable
        # categories and over every category.
        "run_accuracy_comparable": run_accuracy_spread(results, {1, 2, 3, 4}),
        "run_accuracy_all": run_accuracy_spread(results, {1, 2, 3, 4, 5}),
    }

    def _overall(cats: set[int]) -> float | None:
        rows = [r for r in results if r.category in cats and r.label != "ERROR"]
        if not rows:
            return None
        return round(100.0 * sum(r.label == "CORRECT" for r in rows) / len(rows), 2)

    # Dependency distance: accuracy by how many sessions the gold evidence spans, over
    # the comparable categories only (adversarial items cite no evidence and are scored
    # as abstain). Per evidence-session bucket: accuracy, and retrieval COVERAGE — the
    # share of questions whose gold sessions all reached the window (`source_recall_10`
    # == 1, over questions whose hits carried a session). Coverage and accuracy are
    # expected to move together, so a multi-session loss reads as "sessions never
    # retrieved" or "retrieved and misread". `miss_unretrieved` / `miss_retrieved` count
    # the wrong answers each way.
    by_dist: dict[str, dict[str, Any]] = {
        k: {
            "n": 0,
            "correct": 0,
            "coverage_n": 0,
            "gold_in_window": 0,
            "miss_unretrieved": 0,
            "miss_retrieved": 0,
        }
        for k in ("1", "2", "3+")
    }
    for r in results:
        if r.category not in COMPARABLE_CATEGORIES or r.label == "ERROR":
            continue
        b = by_dist[evidence_distance_bucket(r.evidence_sessions)]
        b["n"] += 1
        b["correct"] += r.label == "CORRECT"
        if r.source_recall_10 >= 0:
            b["coverage_n"] += 1
            covered = r.source_recall_10 >= 0.999
            b["gold_in_window"] += covered
            if r.label != "CORRECT":
                b["miss_retrieved" if covered else "miss_unretrieved"] += 1
    for b in by_dist.values():
        b["accuracy"] = round(100.0 * b["correct"] / b["n"], 2) if b["n"] else None
        b["gold_in_window_pct"] = (
            round(100.0 * b["gold_in_window"] / b["coverage_n"], 2) if b["coverage_n"] else None
        )

    # Evidence distance gap (bins) with accuracy AND source recall per
    # bin, so a retrieval fall-off with distance is separable from a reader
    # fall-off. Recall averages only over questions whose hits carried
    # `session_id`; `recall_coverage` says how many did.
    gap_keys = ["1-5", "6-10", "11-15", "16-20", "21-25", "26+"]
    by_gap: dict[str, dict[str, Any]] = {
        k: {"n": 0, "correct": 0, "recall_n": 0, "recall_5": 0.0, "recall_10": 0.0}
        for k in gap_keys
    }
    for r in results:
        if r.category not in COMPARABLE_CATEGORIES or r.label == "ERROR" or r.evidence_gap <= 0:
            continue
        b = by_gap[evidence_gap_bucket(r.evidence_gap)]
        b["n"] += 1
        b["correct"] += r.label == "CORRECT"
        if r.source_recall_10 >= 0:
            b["recall_n"] += 1
            b["recall_5"] += r.source_recall_5
            b["recall_10"] += r.source_recall_10
    for b in by_gap.values():
        b["accuracy"] = round(100.0 * b["correct"] / b["n"], 2) if b["n"] else None
        rn = b.pop("recall_n")
        b["source_recall_5"] = round(100.0 * b["recall_5"] / rn, 1) if rn else None
        b["source_recall_10"] = round(100.0 * b["recall_10"] / rn, 1) if rn else None
        b["recall_coverage"] = rn
        del b["recall_5"], b["recall_10"]

    # Temporal category by gold kind, with the type-aware columns beside the
    # judge: `format_miss` = judged WRONG but the unit-aware
    # date (or ε-duration) matches; `lenient` = judged CORRECT but it does not.
    temporal_by_kind: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.category != 2 or r.label == "ERROR" or not r.temporal_kind:
            continue
        b = temporal_by_kind.setdefault(
            r.temporal_kind,
            {"n": 0, "correct": 0, "typed_match": 0, "format_miss": 0, "lenient": 0},
        )
        b["n"] += 1
        ok = r.label == "CORRECT"
        b["correct"] += ok
        typed = r.date_match if r.temporal_kind in ("date", "year") else r.duration_match
        if typed == "1":
            b["typed_match"] += 1
            if not ok:
                b["format_miss"] += 1
        elif typed == "0" and ok:
            b["lenient"] += 1
    for b in temporal_by_kind.values():
        b["accuracy"] = round(100.0 * b["correct"] / b["n"], 2) if b["n"] else None

    # Abstain both ways: abstaining on adversarial
    # items is the right call, abstaining on an answerable one is a miss the
    # category accuracy already pays for but does not name. `abstain_precision`
    # is the share of abstentions that landed on adversarial items;
    # `false_abstain_rate` is the share of answerable items abstained on.
    judged = [r for r in results if r.label != "ERROR"]
    abstained = [r for r in judged if r.abstained]
    answerable = [r for r in judged if r.category in COMPARABLE_CATEGORIES]
    adv_abstained = sum(1 for r in abstained if r.category not in COMPARABLE_CATEGORIES)
    ans_abstained = sum(1 for r in answerable if r.abstained)

    # Adversarial items by kind (`evidence_speaker`): a speaker swap names
    # the partner of the person who said the evidence (attribution test); a
    # content swap keeps the speaker and changes the fact (premise test).
    # Answerable items split the same way: did the named speaker say the
    # evidence, or did the partner say it about them.
    def _acc(rows: list[QuestionResult]) -> float | None:
        if not rows:
            return None
        return round(100.0 * sum(r.label == "CORRECT" for r in rows) / len(rows), 2)

    adversarial = [r for r in judged if r.category == 5]
    swaps = [r for r in adversarial if r.evidence_speaker == "other"]
    content = [r for r in adversarial if r.evidence_speaker == "named"]
    ans_named = [r for r in answerable if r.evidence_speaker == "named"]
    ans_other = [r for r in answerable if r.evidence_speaker == "other"]
    by_evidence_speaker = {
        "adversarial": {"n": len(adversarial), "accuracy": _acc(adversarial)},
        "adversarial_speaker_swap": {"n": len(swaps), "accuracy": _acc(swaps)},
        "adversarial_content_swap": {"n": len(content), "accuracy": _acc(content)},
        "adversarial_unclassified": len(adversarial) - len(swaps) - len(content),
        "answerable_named_speaker": {"n": len(ans_named), "accuracy": _acc(ans_named)},
        "answerable_other_speaker": {"n": len(ans_other), "accuracy": _acc(ans_other)},
    }
    abstain = {
        "abstained": len(abstained),
        "adversarial_abstained": adv_abstained,
        "answerable_abstained": ans_abstained,
        "answerable": len(answerable),
        "abstain_precision": (
            round(100.0 * adv_abstained / len(abstained), 2) if abstained else None
        ),
        "false_abstain_rate": (
            round(100.0 * ans_abstained / len(answerable), 2) if answerable else None
        ),
    }

    scored = [r for r in results if r.f1 >= 0]
    cat_f1 = [b["f1"] for b in by_cat.values() if b.get("f1") is not None]
    cat_bleu = [b["bleu1"] for b in by_cat.values() if b.get("bleu1") is not None]
    toks_all = [r.context_tokens for r in scored if r.context_tokens >= 0]
    papers_table = {
        "f1_over_questions": (
            round(100.0 * sum(r.f1 for r in scored) / len(scored), 2) if scored else None
        ),
        "bleu1_over_questions": (
            round(100.0 * sum(r.bleu1 for r in scored) / len(scored), 2) if scored else None
        ),
        "f1_over_categories": round(sum(cat_f1) / len(cat_f1), 2) if cat_f1 else None,
        "bleu1_over_categories": round(sum(cat_bleu) / len(cat_bleu), 2) if cat_bleu else None,
        "f1_simplified_over_questions": (
            round(100.0 * sum(r.f1_simplified for r in scored) / len(scored), 2) if scored else None
        ),
        "context_tokens_mean": round(sum(toks_all) / len(toks_all)) if toks_all else None,
        "scored": len(scored),
    }
    return {
        "repeats": repeats_info,
        "recall": recall_info,
        "papers_table": papers_table,
        "by_category": by_cat,
        "by_evidence_sessions": by_dist,
        "by_evidence_gap": by_gap,
        "temporal_by_kind": temporal_by_kind,
        "abstain": abstain,
        "by_evidence_speaker": by_evidence_speaker,
        "overall_comparable": _overall(set(COMPARABLE_CATEGORIES)),
        "overall_all": _overall(set(CATEGORY_NAMES)),
        "judged": sum(1 for r in results if r.label != "ERROR"),
        "errors": sum(1 for r in results if r.label == "ERROR"),
    }


# ---------------------------------------------------------------- HTTP


async def _post(
    client: httpx.AsyncClient, url: str, body: dict[str, Any], token: str, *, retries: int = 7
) -> dict[str, Any]:
    """POST with backoff on 5xx AND on transport errors. A server redeploy
    mid-run (the container restarts for ~30-60 s) otherwise turns every
    in-flight conversation into an errors=N row and the arm is lost; seven
    attempts at 0.5·2^n back off to ~64 s total, which rides out a restart."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=180.0)
        except httpx.TransportError as e:  # connect refused / reset / read timeout
            last_exc = e
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        if r.status_code < 500:
            r.raise_for_status()
            return r.json()
        last_exc = httpx.HTTPStatusError(
            f"{r.status_code} from {url}", request=r.request, response=r
        )
        await asyncio.sleep(0.5 * (2**attempt))
    assert last_exc is not None
    raise last_exc


async def _chat(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    api_key: str,
    *,
    max_tokens: int,
    usage: dict[str, int],
    system: str = "",
) -> str:
    # bench/judges.py shapes the request for the provider and model family
    # (reasoning models get output headroom and no temperature).
    reply, u = await achat(
        client,
        model,
        prompt,
        max_tokens=max_tokens,
        system=system,
        api_key="" if is_claude(model) else api_key,
    )
    usage["tin"] += u["prompt_tokens"]
    usage["tout"] += u["completion_tokens"]
    usage["calls"] += 1
    return reply


# ---------------------------------------------------------------- pipeline


async def ingest_conversation(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    identity: dict[str, str],
    conversation: dict[str, Any],
    *,
    concurrency: int,
    extraction_instructions: str,
    max_sessions: int = 0,
    ingest_json: dict[str, Any] | None = None,
) -> ConversationBuild:
    build = ConversationBuild(sample_id=identity["namespace"], namespace=identity["namespace"])
    sessions = iter_sessions(conversation)
    if max_sessions:
        sessions = sessions[:max_sessions]
    sem = asyncio.Semaphore(max(1, concurrency))
    t0 = time.perf_counter()

    async def _one(key: str, iso: str, turns: list[dict[str, Any]]) -> None:
        body: dict[str, Any] = {
            **identity,
            "session_id": f"{identity['namespace']}:{key}",
            "messages": [turn_to_message(t) for t in turns],
        }
        if iso:
            body["observation_date"] = iso
        if extraction_instructions:
            body["extraction_instructions"] = extraction_instructions
        if ingest_json:
            body.update(ingest_json)
        async with sem:
            try:
                resp = await _post(client, base_url + "/v1/memory.set_from_messages", body, token)
            except httpx.HTTPError as e:
                build.errors.append(f"{key}: {e}")
                return
        build.sessions += 1
        build.turns += len(turns)
        build.written += len(resp.get("written") or [])
        build.tokens_in += int(resp.get("tokens_in", 0))
        build.tokens_out += int(resp.get("tokens_out", 0))
        build.llm_model = str(resp.get("llm_model") or build.llm_model)

    await asyncio.gather(*(_one(k, iso, t) for k, iso, t in sessions))
    build.seconds = round(time.perf_counter() - t0, 1)
    return build


async def answer_question(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    identity: dict[str, str],
    q: dict[str, Any],
    q_index: int,
    *,
    top_k: int,
    search_mode: str,
    reader: str,
    reader_model: str,
    judge_model: str,
    ask_json: dict | None = None,
    api_key: str,
    usage: dict[str, int],
    search_json: dict[str, Any] | None = None,
    reader_prompt: str = "bench",
    credit: bool = False,
    last_session: int = 0,
    policy: str = "default",
    conversation: dict[str, Any] | None = None,
) -> QuestionResult:
    question = str(q.get("question") or "").strip()
    category = int(q.get("category", 0))
    gold = gold_answer(q)
    base = dict(
        sample_id=identity["namespace"],
        namespace=identity["namespace"],
        question_index=q_index,
        category=category,
        category_name=CATEGORY_NAMES.get(category, str(category)),
        question=question,
        gold=gold,
        evidence_sessions=evidence_session_count(q),
        evidence_gap=evidence_gap(q, last_session),
        evidence_speaker=evidence_speaker(q, conversation),
        search_mode=search_mode,
        reader=reader,
        reader_model=reader_model if reader == "external" else "loom:memory.ask",
        judge_model=judge_model,
    )
    generated, hits, embedding_model, abstained, search_ms, top_keys = "", 0, "", False, 0, ""
    context_tokens = -1
    answer_ms = 0
    results: list[dict[str, Any]] = []
    ask_cited: list[str] = []
    ask_tc: float | None = None
    try:
        t0 = time.perf_counter()
        if reader == "ask":
            resp = await _post(
                client,
                base_url + "/v1/memory.ask",
                {
                    **identity,
                    "query": question,
                    "top_k": top_k,
                    **({"search_mode": search_mode} if search_mode else {}),
                    "namespace_scope": "this",
                    "allow_synthesis": True,
                    # An evaluation read credits nothing
                    # unless the run said --credit (merged into ask_json).
                    **measurement_fields("ask", credit=credit),
                    **(
                        {"as_of": conv_asof(conversation)}
                        if os.environ.get("LOCOMO_AS_OF")
                        and conv_asof(conversation)
                        and "as_of" not in (ask_json or {})
                        else {}
                    ),
                    **(ask_json or {}),
                },
                token,
            )
            ask_cited = [str(x) for x in (resp.get("cited_sessions") or [])]
            ask_tc = resp.get("temporal_consistency")
            search_ms = int((time.perf_counter() - t0) * 1000)
            results = list(resp.get("results") or [])
            hits = len(results)
            abstained = bool(resp.get("abstain")) or not (resp.get("answer") or "").strip()
            generated = (resp.get("answer") or "").strip() or _ABSTAIN_TEXT
            context_tokens = int(resp.get("context_tokens_est") or -1)
            top_keys = " | ".join(str(h.get("memory_key", "")) for h in results[:top_k])
        else:
            body = {
                **identity,
                "query": question,
                "top_k": top_k,
                **({"search_mode": search_mode} if search_mode else {}),
                "namespace_scope": "this",
                **measurement_fields("search", credit=credit),
                **(search_json or {}),
            }
            if reader_prompt == "loom":
                body["render"] = "reader"
            resp = await _post(client, base_url + "/v1/memory.search", body, token)
            search_ms = int((time.perf_counter() - t0) * 1000)
            results = list(resp.get("results") or [])
            hits = len(results)
            embedding_model = str(resp.get("embedding_model") or "")
            top_keys = " | ".join(str(h.get("memory_key", "")) for h in results)
            if not results or (reader_prompt == "loom" and resp.get("abstain")):
                # The server's retrieval gate is what memory.ask abstains on;
                # the loom-prompt arm honours it the same way.
                generated, abstained = _ABSTAIN_TEXT, True
            elif reader_prompt == "loom" and resp.get("reader_block"):
                # The server's rendering + instructions: what
                # memory.ask reads, on the bench's external model.
                t1 = time.perf_counter()
                generated = await _chat(
                    client,
                    reader_model,
                    f"Stored memories:\n\n{resp['reader_block']}\n\nQuestion: {question}",
                    api_key,
                    max_tokens=160,
                    usage=usage,
                    system=str(resp.get("reader_instructions") or ""),
                )
                answer_ms = int((time.perf_counter() - t1) * 1000)
                low = generated.lower()
                abstained = _ABSTAIN_TEXT.lower()[:20] in low or (
                    ("do not contain" in low or "does not contain" in low) and len(generated) < 160
                )
            else:
                t1 = time.perf_counter()
                generated = await _chat(
                    client,
                    reader_model,
                    _READER_TEMPLATE.format(history=format_history(results), question=question),
                    api_key,
                    max_tokens=120,
                    usage=usage,
                )
                answer_ms = int((time.perf_counter() - t1) * 1000)
                abstained = _ABSTAIN_TEXT.lower()[:20] in generated.lower()
        if category == 5:
            prompt = _JUDGE_ADVERSARIAL_TEMPLATE.format(question=question, generated=generated)
        else:
            prompt = _JUDGE_TEMPLATE.format(question=question, gold=gold, generated=generated)
        reply = await _chat(client, judge_model, prompt, api_key, max_tokens=20, usage=usage)
        label = parse_judge_label(reply) or "ERROR"
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        label, reply = "ERROR", f"error: {e}"
    # Source recall needs hits that carry `session_id`; without any, the
    # field stays -1 so an older server reads as "not measured", never as 0.
    hit_sessions = hit_session_numbers(results, identity.get("namespace", ""))
    gold_sessions = evidence_sessions_cited(q)
    recall_5 = source_recall_at(hit_sessions, gold_sessions, 5) if hit_sessions else None
    recall_10 = source_recall_at(hit_sessions, gold_sessions, 10) if hit_sessions else None
    f1 = bleu = f1_s = bleu_s = -1.0
    if gold and label != "ERROR":
        f1 = round(token_f1(generated, gold), 4)
        bleu = round(bleu1(generated, gold), 4)
        simplified = simplify_heuristic(generated)
        f1_s = round(token_f1(simplified, gold), 4)
        bleu_s = round(bleu1(simplified, gold), 4)

    t_kind = t_date = t_dur = ""
    if category == 2:
        try:
            from bench.answer_scoring import date_match as _date_match
            from bench.answer_scoring import duration_match as _duration_match
            from bench.answer_scoring import temporal_kind as _temporal_kind
        except ModuleNotFoundError:  # run as `python bench/locomo_qa.py` (sys.path[0] = bench/)
            from answer_scoring import date_match as _date_match  # type: ignore[no-redef]
            from answer_scoring import duration_match as _duration_match  # type: ignore[no-redef]
            from answer_scoring import temporal_kind as _temporal_kind  # type: ignore[no-redef]

        t_kind = _temporal_kind(gold)
        dm = _date_match(generated, gold)
        du = _duration_match(generated, gold)
        t_date = "" if dm is None else ("1" if dm else "0")
        t_dur = "" if du is None else ("1" if du else "0")
    cited_nums = {n for n in (_session_number(x) for x in ask_cited) if n is not None}
    cited_jaccard = (
        len(cited_nums & gold_sessions) / len(cited_nums | gold_sessions)
        if ask_cited and gold_sessions and (cited_nums | gold_sessions)
        else -1.0
    )
    return QuestionResult(
        **base,
        temporal_kind=t_kind,
        date_match=t_date,
        duration_match=t_dur,
        source_recall_5=-1.0 if recall_5 is None else recall_5,
        source_recall_10=-1.0 if recall_10 is None else recall_10,
        hits_with_session=len(hit_sessions),
        cited_jaccard=cited_jaccard,
        temporal_consistency=-9.0 if ask_tc is None else float(ask_tc),
        generated=generated,
        label=label,
        judge_reply=reply,
        abstained=abstained,
        hits=hits,
        search_ms=search_ms,
        answer_ms=answer_ms,
        f1=f1,
        bleu1=bleu,
        f1_simplified=f1_s,
        bleu1_simplified=bleu_s,
        context_tokens=context_tokens,
        embedding_model=embedding_model,
        top_keys=top_keys,
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Write-policy arms, paired comparison and per-operation provenance. A memory
# operation is judged by downstream answer correctness, used here as an
# evaluation: ingest each conversation once per policy, grade the same
# questions, and attribute every flipped verdict to the write decision that
# changed the memory set.
# ---------------------------------------------------------------------------

POLICY_DEFAULT = "default"


def parse_policies(raw: str) -> list[str]:
    """`--policy a,b` → ordered, de-duplicated arms; empty → ["default"]."""
    out: list[str] = []
    for part in (raw or "").split(","):
        arm = part.strip()
        if arm and arm not in out:
            out.append(arm)
    return out or [POLICY_DEFAULT]


def arm_namespace(prefix: str, arm: str, sample_id: str, nonce: str) -> str:
    """Default arm keeps the historical name so old runs stay comparable; a
    policy arm names itself so two arms never share a namespace."""
    if arm == POLICY_DEFAULT:
        return f"{prefix}-{sample_id}-{nonce}"
    return f"{prefix}-{arm}-{sample_id}-{nonce}"


def register_extra_for(arm: str) -> dict[str, Any] | None:
    """namespace.register fields for the arm: the conflict policy, or nothing."""
    return None if arm == POLICY_DEFAULT else {"default_conflict_policy": arm}


def _load_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _qkey(r: dict[str, str]) -> tuple[str, str]:
    return (str(r.get("sample_id", "")), str(r.get("question_index", "")))


def sign_test_p(up: int, down: int) -> float:
    """Exact two-sided sign test on the discordant pairs (McNemar's exact
    form): the probability, under "the two arms are the same system", of a
    split at least this lopsided among the `up + down` questions whose verdict
    changed. Concordant pairs carry no information and are not counted. 1.0
    when nothing flipped."""
    n = up + down
    if n == 0:
        return 1.0
    lo = min(up, down)
    tail = sum(math.comb(n, i) for i in range(0, lo + 1)) / 2**n
    return min(1.0, 2.0 * tail)


def _text_key(r: dict[str, str]) -> tuple[str, str]:
    return (str(r.get("question_index", "")), str(r.get("question", "")))


def paired_compare(rows_a: list[dict[str, str]], rows_b: list[dict[str, str]]) -> dict[str, Any]:
    """Paired judge-accuracy delta (B minus A) over questions present in both
    files, clustered by conversation for the bootstrap; flips listed both ways.

    Rows pair by (sample_id, question_index); when none do (a file whose
    sample_id column holds a per-run namespace), they pair by
    (question_index, question text) instead.

    Beside the cluster bootstrap — which collapses to the point estimate when
    the arms hold one conversation — the report carries the exact sign test
    on the discordant pairs (`sign_test_p`), the per-category counts, and
    `same_context_flips`: flips whose two answers were built from the same
    number of context tokens, i.e. the retrieval did not change and the
    synthesizer re-rolled. On LoCoMo conversation 26 two runs of identical
    code differed by 6 of 199 (p 0.21), so a single-conversation delta under
    about ±4 pp with p above 0.05 is the bench's noise, not the change."""
    try:
        from bench.bootstrap import paired_bootstrap
    except ModuleNotFoundError:  # `python bench/locomo_qa.py` puts bench/ on sys.path
        from bootstrap import paired_bootstrap  # type: ignore[no-redef]
    keyf = _qkey
    b_by = {keyf(r): r for r in rows_b}
    if not any(keyf(ra) in b_by for ra in rows_a):
        keyf = _text_key
        b_by = {keyf(r): r for r in rows_b}
    deltas: list[float] = []
    clusters: list[str] = []
    a_wrong_b_right: list[tuple[str, str]] = []
    a_right_b_wrong: list[tuple[str, str]] = []
    same_context_flips = 0
    by_cat: dict[str, dict[str, int]] = {}
    for ra in rows_a:
        rb = b_by.get(keyf(ra))
        if rb is None:
            continue
        ca = 1.0 if ra.get("label") == "CORRECT" else 0.0
        cb = 1.0 if rb.get("label") == "CORRECT" else 0.0
        deltas.append(cb - ca)
        clusters.append(str(ra.get("sample_id", "")))
        cat = str(ra.get("category_name") or ra.get("category") or "")
        c = by_cat.setdefault(cat, {"n": 0, "a": 0, "b": 0, "up": 0, "down": 0})
        c["n"] += 1
        c["a"] += int(ca)
        c["b"] += int(cb)
        if ca != cb and str(ra.get("context_tokens", "")) == str(rb.get("context_tokens", "")):
            same_context_flips += 1
        if ca < cb:
            a_wrong_b_right.append(keyf(ra))
            c["up"] += 1
        elif ca > cb:
            a_right_b_wrong.append(keyf(ra))
            c["down"] += 1
    boot = paired_bootstrap(deltas, clusters) if deltas else None
    n = len(deltas)
    correct_a = sum(1 for ra in rows_a if keyf(ra) in b_by and ra.get("label") == "CORRECT")
    acc_a = (correct_a / n) if n else 0.0
    acc_b = acc_a + (sum(deltas) / n if n else 0.0)
    up, down = len(a_wrong_b_right), len(a_right_b_wrong)
    paired_a = [ra for ra in rows_a if keyf(ra) in b_by]
    paired_b = [b_by[keyf(ra)] for ra in paired_a]
    return {
        "paired_questions": n,
        # Each arm's per-item flip rate over its --repeats
        # labels (None for single-trial CSVs) beside the delta it qualifies.
        "flip_rate_a": flip_rate(str(r.get("repeat_labels", "")).split("|") for r in paired_a),
        "flip_rate_b": flip_rate(str(r.get("repeat_labels", "")).split("|") for r in paired_b),
        "paired_by": "sample_id" if keyf is _qkey else "question_text",
        "conversations": len(set(clusters)),
        "policy_a": next((r.get("policy", "") for r in rows_a), ""),
        "policy_b": next((r.get("policy", "") for r in rows_b), ""),
        "accuracy_a": round(acc_a, 4),
        "accuracy_b": round(acc_b, 4),
        "delta_pp": round(100.0 * (sum(deltas) / n), 2) if n else 0.0,
        "ci95_pp": [round(100.0 * boot.lb, 2), round(100.0 * boot.ub, 2)] if boot else None,
        "up": up,
        "down": down,
        "sign_test_p": round(sign_test_p(up, down), 4),
        "same_context_flips": same_context_flips,
        "by_category": by_cat,
        "a_wrong_b_right": a_wrong_b_right,
        "a_right_b_wrong": a_right_b_wrong,
    }


def attribute_flips(
    rows_a: list[dict[str, str]],
    rows_b: list[dict[str, str]],
    mem_rows: dict[str, dict[str, dict[str, Any]]],
    drop_rows: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """For every question whose verdict differs between the arms, name the
    write decision behind the cited memories in each arm.

    `mem_rows[namespace][memory_key]` → {status, superseded_by, invalid_at};
    `drop_rows[namespace]` → [{stage, reason, item}]. Per arm the attribution
    is the strongest of: `superseded` (a cited key's live row was retired),
    `dropped_in_batch` (a same-key fact was dropped at ingest,
    `superseded_in_batch`), `conflicted` / `duplicate` (flagged, still
    read), else `intact`. Pure; the CH fetch lives in fetch_provenance."""
    b_by = {_qkey(r): r for r in rows_b}
    out: list[dict[str, Any]] = []

    def _side(r: dict[str, str]) -> dict[str, Any]:
        ns = str(r.get("namespace", ""))
        keys = [k.strip() for k in str(r.get("top_keys", "")).split("|") if k.strip()]
        rows = mem_rows.get(ns, {})
        drops = drop_rows.get(ns, [])
        statuses = {k: str((rows.get(k) or {}).get("status", "")) for k in keys}
        dropped = sorted(
            {
                str(d.get("item", ""))
                for d in drops
                if str(d.get("reason", "")) == "superseded_in_batch"
                and any(str(d.get("item", "")).startswith(k) for k in keys)
            }
        )
        superseded = [k for k, st in statuses.items() if st == "superseded"]
        flagged = [k for k, st in statuses.items() if st in ("conflicted", "duplicate")]
        if superseded:
            decision = "superseded"
        elif dropped:
            decision = "dropped_in_batch"
        elif flagged:
            decision = statuses[flagged[0]]
        else:
            decision = "intact"
        return {
            "namespace": ns,
            "label": r.get("label", ""),
            "keys": keys,
            "statuses": statuses,
            "superseded": superseded,
            "dropped_in_batch": dropped,
            "decision": decision,
        }

    for ra in rows_a:
        rb = b_by.get(_qkey(ra))
        if rb is None or ra.get("label") == rb.get("label"):
            continue
        out.append(
            {
                "sample_id": ra.get("sample_id", ""),
                "question_index": ra.get("question_index", ""),
                "category": ra.get("category_name", ""),
                "question": ra.get("question", ""),
                "gold": ra.get("gold", ""),
                "a": _side(ra),
                "b": _side(rb),
            }
        )
    return out


async def fetch_provenance(
    rows_a: list[dict[str, str]], rows_b: list[dict[str, str]]
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """loom.memory status/superseded_by/invalid_at for every cited key and the
    memory_drops rows, per namespace of both arms. Uses the server's own
    ClickHouse client (run from the repo root with .env present)."""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    by_ns: dict[str, set[str]] = {}
    for r in rows_a + rows_b:
        ns = str(r.get("namespace", ""))
        keys = [k.strip() for k in str(r.get("top_keys", "")).split("|") if k.strip()]
        if ns:
            by_ns.setdefault(ns, set()).update(keys)
    mem: dict[str, dict[str, dict[str, Any]]] = {}
    drops: dict[str, list[dict[str, Any]]] = {}
    for ns, keys in by_ns.items():
        rows = await ch.query_all(
            """
            SELECT memory_key, toString(status) AS status, superseded_by,
                   toString(invalid_at) AS invalid_at
            FROM loom.memory FINAL
            WHERE namespace = {ns:String} AND memory_key IN {keys:Array(String)}
            """,
            params={"ns": ns, "keys": sorted(keys)},
        )
        mem[ns] = {str(r["memory_key"]): dict(r) for r in rows}
        drows = await ch.query_all(
            """
            SELECT stage, reason, item FROM loom.memory_drops
            WHERE namespace = {ns:String}
            """,
            params={"ns": ns},
        )
        drops[ns] = [dict(r) for r in drows]
    return mem, drops


def compare_command(path_a: str, path_b: str, args: argparse.Namespace | None = None) -> int:
    rows_a, rows_b = _load_rows(path_a), _load_rows(path_b)
    report = paired_compare(rows_a, rows_b)
    if args is not None and getattr(args, "gate", False):
        # The verdict every accuracy gate reads — the paired
        # clustered bootstrap over the per-question MAJORITY labels (the
        # `repeat_labels` cells when the CSVs carry them), the flip rate
        # beside it, and pass|fail|unresolved against the measured band.
        key = ("sample_id", "question_index") if report["paired_by"] == "sample_id" else "question"
        gate = gate_from_args(
            args,
            labels_by_key(rows_a, key=key),
            labels_by_key(rows_b, key=key),
            clusters=group_by(rows_a, key=key, field="sample_id"),
        )
        report["gate"] = gate.as_dict()
        print("\n".join(gate.lines()))
    print(json.dumps(report, indent=1))
    return 0


async def provenance_command(path_a: str, path_b: str, out: str) -> int:
    rows_a, rows_b = _load_rows(path_a), _load_rows(path_b)
    mem, drops = await fetch_provenance(rows_a, rows_b)
    flips = attribute_flips(rows_a, rows_b, mem, drops)
    with open(out, "w") as f:
        for fl in flips:
            f.write(json.dumps(fl, default=str) + "\n")
    from collections import Counter

    print(
        json.dumps(
            {
                "flips": len(flips),
                "decision_a": dict(Counter(fl["a"]["decision"] for fl in flips)),
                "decision_b": dict(Counter(fl["b"]["decision"] for fl in flips)),
                "out": out,
            },
            indent=1,
        )
    )
    return 0


async def run(args: argparse.Namespace) -> int:
    ask_json = json.loads(args.ask_json) if getattr(args, "ask_json", "") else None
    if args.reader == "ask" and int(getattr(args, "repeats", 1) or 1) > 1:
        # --repeats measures the reader's own variance; a served cache hit
        # would repeat the first answer's bytes. Off unless
        # the arm pinned it.
        ask_json = {"llm_cache": False, **(ask_json or {})}
    data = json.loads(Path(args.dataset).read_text())
    if not isinstance(data, list):
        print("dataset must be the locomo10.json list")
        return 2
    if args.limit_conversations:
        data = data[: args.limit_conversations]
    wanted = (
        {int(c) for c in args.categories.split(",")} if args.categories else set(CATEGORY_NAMES)
    )
    # --only-kinds: restrict to questions whose GOLD is a date / year /
    # duration / other (the temporal kinds), for paired
    # runs that target one loss bucket without paying for the whole category.
    only_kinds = {k.strip() for k in args.only_kinds.split(",") if k.strip()}
    if only_kinds:
        try:
            from bench.answer_scoring import temporal_kind as _kind_of_gold
        except ModuleNotFoundError:  # run as a script from bench/
            from answer_scoring import temporal_kind as _kind_of_gold  # type: ignore[no-redef]
    else:
        _kind_of_gold = None

    api_key = os.environ.get("BENCH_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY (or BENCH_OPENAI_API_KEY) is required for reader + judge")
        return 2

    reuse: dict[str, str] = {}
    if args.namespaces_json and Path(args.namespaces_json).exists():
        reuse = json.loads(Path(args.namespaces_json).read_text())

    builds: list[ConversationBuild] = []
    results: list[QuestionResult] = []
    usage = {"tin": 0, "tout": 0, "calls": 0}
    namespaces: dict[str, str] = dict(reuse)

    arms = parse_policies(getattr(args, "policy", "") or "")

    def post(path: str, body: dict[str, Any]) -> Any:
        return _post(client, args.base_url + path, body, args.token)

    # Freshly minted namespaces are erased when the run leaves the client block,
    # after a failure or Ctrl-C too; reused and map-kept ones were never remembered.
    async with (
        httpx.AsyncClient() as client,
        erase_on_exit(post, org=args.org, keep=args.keep_namespaces),
    ):
        for arm in arms:
            for conv in data:
                sample_id = str(conv.get("sample_id") or uuid.uuid4().hex[:8])
                # The default arm keeps the historical map key; a policy arm is
                # keyed "<policy>:<sample_id>" so a reuse file can hold both.
                map_key = sample_id if arm == POLICY_DEFAULT else f"{arm}:{sample_id}"
                namespace = reuse.get(map_key) or arm_namespace(
                    args.namespace_prefix, arm, sample_id, uuid.uuid4().hex[:6]
                )
                namespaces[map_key] = namespace
                identity = {"org": args.org, "namespace": namespace, "agent": "locomo-bench"}
                if map_key not in reuse:
                    # A namespace written into the --namespaces-json map is for a
                    # later run to reuse: kept, and registered out of the idle gc.
                    keep_ns = bool(args.keep_namespaces or args.namespaces_json)
                    if not keep_ns:
                        remember_bench_namespace(args.org, namespace)  # erased on exit
                    # Query capture on unless --no-capture-consent, so its trace rows
                    # can feed the relied-on flywheel; the arm's policy rides along.
                    await register_bench_namespace(
                        post,
                        org=args.org,
                        namespace=namespace,
                        description=f"LoCoMo conversation {sample_id} [{arm}]",
                        capture=not args.no_capture_consent,
                        keep=keep_ns,
                        extra=register_extra_for(arm),
                    )
                    build = await ingest_conversation(
                        client,
                        args.base_url,
                        args.token,
                        identity,
                        conv.get("conversation") or {},
                        concurrency=args.ingest_concurrency,
                        extraction_instructions=args.extraction_instructions,
                        max_sessions=args.limit_sessions,
                        ingest_json=json.loads(args.ingest_json) if args.ingest_json else None,
                    )
                    build.sample_id = sample_id
                    builds.append(build)
                    print(
                        f"[{sample_id}] ingested {build.sessions} sessions / {build.turns} turns → "
                        f"{build.written} memories, {build.tokens_in}+{build.tokens_out} tokens, "
                        f"{build.seconds}s, errors={len(build.errors)}",
                        flush=True,
                    )
                    if build.errors:
                        # A half-ingested namespace must not be reused: drop it from
                        # the map so a re-run with --namespaces-json ingests it again,
                        # and erase it (it was kept for the map).
                        namespaces.pop(map_key, None)
                        if keep_ns and not args.keep_namespaces:
                            await erase_bench_namespaces(post, org=args.org, namespaces=[namespace])
                    if args.namespaces_json:
                        Path(args.namespaces_json).write_text(json.dumps(namespaces, indent=1))
                qs = [
                    (i, q)
                    for i, q in enumerate(conv.get("qa") or [])
                    if int(q.get("category", 0)) in wanted
                    and (
                        _kind_of_gold is None
                        or _kind_of_gold(str(q.get("answer", ""))) in only_kinds
                    )
                ]
                # The query's session for the evidence-gap axis: LoCoMo asks after
                # the whole conversation, so it is the last session that exists.
                # The sessions live under the sample's `conversation` key; the
                # sample itself has none.
                conversation_body = conv.get("conversation") or conv
                _conv_sessions = iter_sessions(conversation_body)
                last_session = max((int(k.split("_")[1]) for k, _, _ in _conv_sessions), default=0)
                if args.limit_questions:
                    qs = qs[: args.limit_questions]
                qa_sem = asyncio.Semaphore(max(1, args.qa_concurrency))

                async def _guarded(
                    i: int,
                    q: dict[str, Any],
                    _identity: dict[str, str] = identity,
                    _sem: asyncio.Semaphore = qa_sem,
                    _last_session: int = last_session,
                    _arm: str = arm,
                    _conversation: dict[str, Any] = conversation_body,
                ) -> QuestionResult:
                    async with _sem:
                        runs: list[QuestionResult] = []
                        for _rep in range(max(1, int(getattr(args, "repeats", 1) or 1))):
                            runs.append(
                                await answer_question(
                                    client,
                                    args.base_url,
                                    args.token,
                                    _identity,
                                    q,
                                    i,
                                    top_k=args.top_k,
                                    search_mode=args.search_mode,
                                    reader=args.reader,
                                    ask_json=ask_json,
                                    reader_model=args.reader_model,
                                    judge_model=args.judge_model,
                                    api_key=api_key,
                                    usage=usage,
                                    search_json=(
                                        json.loads(args.search_json) if args.search_json else None
                                    ),
                                    reader_prompt=args.reader_prompt,
                                    credit=bool(getattr(args, "credit", False)),
                                    last_session=_last_session,
                                    policy=_arm,
                                    conversation=_conversation,
                                )
                            )
                        return runs[0] if len(runs) == 1 else fold_repeats(runs)

                conv_results = await asyncio.gather(*(_guarded(i, q) for i, q in qs))
                # answer_question knows only the namespace, which carries a per-run
                # nonce; the row's sample_id is the dataset's, so fresh runs pair.
                for r in conv_results:
                    r.sample_id = sample_id
                results.extend(conv_results)
                agg = aggregate(list(conv_results))
                print(
                    f"[{sample_id}][{arm}] {len(conv_results)} questions: "
                    f"comparable={agg['overall_comparable']} all={agg['overall_all']} "
                    f"errors={agg['errors']}"
                    + (
                        f" unstable={agg['repeats']['unstable']}"
                        f"/{agg['repeats']['questions_repeated']}"
                        if agg["repeats"]["questions_repeated"]
                        else ""
                    ),
                    flush=True,
                )

        # Second judge (--second-judge-model): re-grade an evenly spaced sample
        # of the judged answers with an independent model, same prompt, and
        # report agreement + kappa in the summary. The judge is the one part
        # of this bench's noise band that a second reading can size.
        if args.second_judge_model:
            sample = second_judge_sample(results, int(args.second_judge_sample))
            sj_sem = asyncio.Semaphore(max(1, args.qa_concurrency))

            async def _rejudge(r: QuestionResult) -> None:
                if r.category == 5:
                    prompt = _JUDGE_ADVERSARIAL_TEMPLATE.format(
                        question=r.question, generated=r.generated
                    )
                else:
                    prompt = _JUDGE_TEMPLATE.format(
                        question=r.question, gold=r.gold, generated=r.generated
                    )
                async with sj_sem:
                    try:
                        reply = await _chat(
                            client,
                            args.second_judge_model,
                            prompt,
                            api_key,
                            max_tokens=20,
                            usage=usage,
                        )
                        r.second_judge_label = parse_judge_label(reply) or "ERROR"
                    except (httpx.HTTPError, KeyError, IndexError, ValueError):
                        r.second_judge_label = "ERROR"

            await asyncio.gather(*(_rejudge(r) for r in sample))
            ja = judge_agreement_summary(results, args.second_judge_model)
            print(
                f"[second judge {args.second_judge_model}] sampled={ja['sampled']} "
                f"agreement={ja['agreement']} kappa={ja['kappa']}",
                flush=True,
            )

        # Deployment footprint: rows and estimated
        # tokens stored per conversation, read back from memory.health so a
        # --namespaces-json re-score reports it too. One call per namespace.
        footprints: dict[str, dict[str, Any]] = {}
        if not getattr(args, "no_footprint", False):
            for map_key, ns in namespaces.items():
                footprints[map_key] = await namespace_footprint(
                    client,
                    args.base_url,
                    args.token,
                    {"org": args.org, "namespace": ns, "agent": "locomo-bench"},
                )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()) if results else ["empty"])
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))

    agg = aggregate(results)
    search_ms = [r.search_ms for r in results if r.search_ms]
    total_ms = [r.search_ms + r.answer_ms for r in results if r.search_ms]
    answer_ms = [r.answer_ms for r in results if r.answer_ms]
    measured_fp = [fp for fp in footprints.values() if not fp.get("error")]
    measured_tok = [fp for fp in measured_fp if fp.get("tokens_est") is not None]
    summary = {
        "dataset": str(args.dataset),
        "conversations": len(data),
        "questions": len(results),
        "reader": args.reader,
        "reader_model": args.reader_model if args.reader == "external" else "loom:memory.ask",
        "reader_prompt": args.reader_prompt if args.reader == "external" else "",
        "judge_model": args.judge_model,
        "search_mode": args.search_mode,
        "top_k": args.top_k,
        "embedding_model": next((r.embedding_model for r in results if r.embedding_model), ""),
        "extraction_instructions": bool(args.extraction_instructions),
        "ingest_json": args.ingest_json,
        "search_json": args.search_json,
        "ingest_concurrency": args.ingest_concurrency,
        "policies": arms,
        "write_path": {
            "conversations_ingested": len(builds),
            "sessions": sum(b.sessions for b in builds),
            "memories_written": sum(b.written for b in builds),
            "extraction_tokens_in": sum(b.tokens_in for b in builds),
            "extraction_tokens_out": sum(b.tokens_out for b in builds),
            "extraction_model": next((b.llm_model for b in builds if b.llm_model), ""),
            "ingest_errors": sum(len(b.errors) for b in builds),
            # Construction wall time (all conversations, at the configured
            # ingest concurrency). Zero on a --namespaces-json re-score.
            "ingest_seconds": round(sum(b.seconds for b in builds), 1),
        },
        # The scoring protocol, so a number from this file is never set next
        # to a published one without saying whether they measure the same
        # thing. Some published numbers use a 0.0–1.0 partial-credit judge and
        # score an adversarial item 1.0 when the answer calls it unanswerable;
        # this file is binary CORRECT / WRONG on the gold
        # answer, and an adversarial item is CORRECT only when the reader
        # abstains. A mean of partial credits and a binary accuracy are
        # different quantities.
        "judge_agreement": (
            judge_agreement_summary(results, args.second_judge_model)
            if args.second_judge_model
            else None
        ),
        "judge_protocol": {
            "scale": "binary CORRECT/WRONG against the gold answer",
            "adversarial": "abstain scores CORRECT; any assertion scores WRONG",
            "reader_model": args.reader_model,
            "judge_model": args.judge_model,
            "comparable_published_column": "categories 1-4 (papers exclude adversarial)",
        },
        "read_path": {
            "reader_judge_calls": usage["calls"],
            "reader_judge_tokens_in": usage["tin"],
            "reader_judge_tokens_out": usage["tout"],
            # Utility per token is the number a k / rerank / budget arm has
            # to move; accuracy alone hides a 67% larger prompt.
            "reader_judge_tokens_per_question": (
                round((usage["tin"] + usage["tout"]) / len(results), 1) if results else None
            ),
            "search_p50_ms": int(statistics.median(search_ms)) if search_ms else None,
            # Deployment columns: search p50/p95 and total (search + answer
            # generation, judge excluded) p50/p95, per question. With
            # --reader ask, `search_ms` is the whole memory.ask call, so the
            # search and total columns coincide.
            "search_p95_ms": percentile_ms(search_ms, 95),
            "answer_p50_ms": int(statistics.median(answer_ms)) if answer_ms else None,
            "total_p50_ms": percentile_ms(total_ms, 50),
            "total_p95_ms": percentile_ms(total_ms, 95),
            "search_seconds_total": round(sum(search_ms) / 1000.0, 1),
        },
        # Stored footprint per conversation (the retrieved-tokens-per-answer
        # column is `context_tokens_mean` above).
        # `tokens_est` is the server's chars/4 estimate over every live
        # (active) value — not a tokenizer count.
        "footprint": {
            "token_estimate": "chars/4 (memory.health footprint.value_tokens_est)",
            "conversations_measured": len(measured_fp),
            "memories_per_conversation_mean": (
                round(sum(fp["memories"] for fp in measured_fp) / len(measured_fp), 1)
                if measured_fp
                else None
            ),
            "memory_tokens_per_conversation_mean": (
                round(sum(fp["tokens_est"] for fp in measured_tok) / len(measured_tok))
                if measured_tok
                else None
            ),
            "per_namespace": footprints,
        },
        # Memory construction time plus query time, amortised per question — the x-axis
        # of a cost-utility frontier. The construction share says whether the write or
        # the read path is the cost. Only meaningful on a run that ingested; a re-score
        # reports the read side alone and construction_share 0.
        "operation_latency_per_query_s": (
            round((sum(b.seconds for b in builds) + sum(search_ms) / 1000.0) / len(results), 2)
            if results
            else None
        ),
        "construction_share": (
            round(
                sum(b.seconds for b in builds)
                / max(1e-9, sum(b.seconds for b in builds) + sum(search_ms) / 1000.0),
                3,
            )
            if results
            else None
        ),
        **agg,
        "namespaces": namespaces,
    }
    out.with_suffix(".summary.json").write_text(json.dumps(summary, indent=1))

    print("\ncategory        n    acc%   abstain%   top-k plane mix (digest/chunk/period/fact)")
    for cat_id in sorted(CATEGORY_NAMES):
        name = CATEGORY_NAMES[cat_id]
        b = agg["by_category"].get(name)
        if not b:
            continue
        acc = b["accuracy"] if b["accuracy"] is not None else "-"
        # None when every question of the category errored.
        abst = b["abstain_rate"] if b["abstain_rate"] is not None else "-"
        mix = " ".join(f"{p}={v:.2f}" for p, v in (b.get("plane_mix") or {}).items())
        print(f"{name:<14} {b['n']:>4}  {acc:>6}  {abst:>6}   {mix}")
    print(
        "\nevidence sessions   n    acc%   gold-in-window%   misses unretrieved/retrieved"
        "   (categories 1-4)"
    )
    for k in ("1", "2", "3+"):
        b = agg["by_evidence_sessions"][k]
        acc = b["accuracy"] if b["accuracy"] is not None else "-"
        cov = b["gold_in_window_pct"] if b["gold_in_window_pct"] is not None else "-"
        print(
            f"{k:<17} {b['n']:>4}  {acc:>6}  {cov:>15}   "
            f"{b['miss_unretrieved']:>10}/{b['miss_retrieved']}"
        )
    ab = agg["abstain"]
    print(
        f"\nabstain: precision={ab['abstain_precision']} "
        f"({ab['adversarial_abstained']}/{ab['abstained']} abstentions were adversarial)  "
        f"false_abstain_rate={ab['false_abstain_rate']} "
        f"({ab['answerable_abstained']}/{ab['answerable']} answerable items abstained)"
    )
    es = agg["by_evidence_speaker"]
    if es["adversarial"]["n"]:
        sw, ct = es["adversarial_speaker_swap"], es["adversarial_content_swap"]
        an, ao = es["answerable_named_speaker"], es["answerable_other_speaker"]
        print(
            f"adversarial by kind: speaker swap={sw['accuracy']} ({sw['n']})  "
            f"content swap={ct['accuracy']} ({ct['n']})  "
            f"unclassified={es['adversarial_unclassified']}   "
            f"answerable: named speaker said it={an['accuracy']} ({an['n']})  "
            f"partner said it={ao['accuracy']} ({ao['n']})"
        )
    rp = summary["read_path"]
    fp = summary["footprint"]
    print(
        f"\nlatency ms: search p50={rp['search_p50_ms']} p95={rp['search_p95_ms']}  "
        f"total p50={rp['total_p50_ms']} p95={rp['total_p95_ms']}"
        + ("  (reader ask: search == total)" if args.reader == "ask" else "")
        + f"\nfootprint: {fp['memories_per_conversation_mean']} memories / "
        f"{fp['memory_tokens_per_conversation_mean']} tokens (chars/4) per conversation"
        f" over {fp['conversations_measured']} namespaces"
    )
    spread = agg["repeats"]["run_accuracy_comparable"]
    if spread["runs"]:
        rp_ = agg["repeats"]
        print(
            f"repeats: comparable accuracy per run {spread['accuracies']} "
            f"→ mean {spread['mean']} ± {spread['sd']} sd; per-item flip rate "
            f"{rp_['flip_rate']} (identical window {rp_['flip_rate_identical_window']}, "
            f"changed window {rp_['flip_rate_changed_window']}); "
            f"top-k window identical across repeats on {rp_['window_identical_rate']} of items"
        )
    print(
        f"\noverall_comparable (cat 1-4, the published column) = {agg['overall_comparable']}\n"
        f"overall_all (cat 1-5, adversarial scored as abstain)  = {agg['overall_all']}\n"
        f"errors = {agg['errors']}   → {out} , {out.with_suffix('.summary.json')}"
    )
    if args.reader_model != _DEFAULT_READER_MODEL or args.judge_model != _DEFAULT_JUDGE_MODEL:
        print(
            "note: reader/judge differ from the pinned defaults (gpt-4.1-mini / gpt-4.1); "
            "the number is not directly comparable."
        )
    if not any(r.label != "ERROR" for r in results):
        # ERROR rows leave the accuracy denominator, so a run that judged nothing
        # would print a summary that looks like a measurement.
        print("no question was judged; see the errors above")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--dataset", default="", help="path to locomo10.json (not needed by --compare/--provenance)"
    )
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--org", default="dev")
    p.add_argument("--namespace-prefix", default="locomo")
    p.add_argument(
        "--no-capture-consent",
        action="store_true",
        help="Do not register each fresh namespace with capture_generation_io=true "
        "(default registers it so search_trace_log keeps the query text).",
    )
    p.add_argument(
        "--namespaces-json", default="", help="sample_id→namespace map; reuse skips ingest"
    )
    p.add_argument(
        "--no-footprint",
        action="store_true",
        help="skip the per-namespace memory.list footprint probe at the end of the run",
    )
    p.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Do not erase this run's fresh namespaces at the end (default erases them with "
        "one list-scoped, purging erasure); the ones written into --namespaces-json are "
        "always kept",
    )
    p.add_argument("--limit-conversations", type=int, default=0)
    p.add_argument("--limit-sessions", type=int, default=0, help="per conversation; smoke runs")
    p.add_argument("--limit-questions", type=int, default=0, help="per conversation; smoke runs")
    p.add_argument("--categories", default="", help="comma list of 1-5; default all")
    p.add_argument(
        "--only-kinds",
        default="",
        help="comma list of gold temporal kinds (date,year,duration,other); default all",
    )
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument(
        "--search-mode",
        default="",
        help=(
            "memory.search search_mode to PIN for every call; empty (default) omits the "
            "field so the server's deployment default applies (semantic). Pinning a "
            "mode measures what production callers do not get by default."
        ),
    )
    p.add_argument("--reader", choices=["external", "ask"], default="external")
    p.add_argument(
        "--reader-prompt",
        choices=["bench", "loom"],
        default="bench",
        help="external reader only: 'bench' = this file's plain template; 'loom' = "
        "memory.search(render='reader') and the server's reader_block + "
        "reader_instructions, same external model",
    )
    p.add_argument(
        "--ask-json",
        default="",
        help="JSON merged into every memory.ask body under --reader ask "
        '(e.g. \'{"decompose": "always"}\' for the decompose arm)',
    )
    p.add_argument("--reader-model", default=_DEFAULT_READER_MODEL)
    p.add_argument("--judge-model", default=_DEFAULT_JUDGE_MODEL)
    p.add_argument(
        "--second-judge-model",
        default="",
        help="re-judge a deterministic sample of the judged questions with this model and "
        "report agreement + Cohen's kappa against --judge-model (a judge is quoted with "
        "its agreement); '' = off",
    )
    p.add_argument(
        "--second-judge-sample",
        type=int,
        default=60,
        help="questions the second judge re-grades (evenly spaced over the judged set)",
    )
    p.add_argument(
        "--extraction-instructions", default="", help="per-call extractor suffix (disclosed tuning)"
    )
    p.add_argument(
        "--search-json",
        default="",
        help='extra memory.search fields for an A/B arm, e.g. \'{"rerank": "builtin:openai"}\'',
    )
    p.add_argument(
        "--ingest-json",
        default="",
        help="extra set_from_messages fields for an A/B arm, e.g. '{\"coverage_sweep\": true}'",
    )
    p.add_argument(
        "--ingest-concurrency",
        type=int,
        default=1,
        help=(
            "sessions extracted at once per conversation. Default 1 = session-date "
            "order, so a later session's extraction sees the earlier facts in "
            "existing_memories (multi-hop 57.1 → 62.4 vs 4). Raise it "
            "only to measure the cost of concurrent ingest."
        ),
    )
    p.add_argument("--qa-concurrency", type=int, default=8)
    p.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="answer+judge each question N times and keep the per-question majority "
        "label (tie = WRONG); the CSV records every label and the agreement, the "
        "summary prints how many questions were unstable and the accuracy per run as "
        "mean ± sd. Two identical single "
        "runs differed by up to 4.9 points on 164 questions; use 3 before reading an arm.",
    )
    p.add_argument("--out", default="bench/locomo_qa.csv")
    p.add_argument(
        "--policy",
        default="",
        help="write-policy arm(s), comma list: default | newest_wins | auto_reinforce | "
        "manual_review. Each arm ingests every conversation into its own namespace "
        "(registered with default_conflict_policy=<arm>) and grades the same "
        "questions; the `policy` column names the arm. Empty = default only.",
    )
    p.add_argument(
        "--compare",
        nargs=2,
        metavar=("A.csv", "B.csv"),
        help="no ingest: paired A/B of two result CSVs — accuracy delta B−A, "
        "cluster-bootstrap CI, exact sign test on the flips, per-category counts, "
        "same-context flips; rows pair by (sample_id, question_index) or, across "
        "fresh namespaces, by question text.",
    )
    p.add_argument(
        "--provenance",
        nargs=2,
        metavar=("A.csv", "B.csv"),
        help="no ingest: for every question whose verdict differs, read the cited "
        "memories' status/superseded_by and the namespace's memory_drops from "
        "ClickHouse (repo root, .env) and write flips.jsonl naming the write "
        "decision behind each side (--out names the file).",
    )
    add_gate_arguments(p, repeats=False)
    args = p.parse_args()
    if args.compare:
        return compare_command(*args.compare, args=args)
    if args.provenance:
        out = args.out if args.out != "bench/locomo_qa.csv" else "bench/locomo_flips.jsonl"
        return asyncio.run(provenance_command(*args.provenance, out))
    if not args.dataset:
        p.error("--dataset is required unless --compare or --provenance")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
