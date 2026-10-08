#!/usr/bin/env python3
"""Multi-turn dialogue consistency through loom — scored by turn, not by recall.

Every other bench here scores retrieval on held-out questions. None runs a
dialogue AUTOREGRESSIVELY (loom's own answer becomes the next assistant
turn), and none plots quality by turn index — so a consistency curve that
decays after the first few turns is a shape loom could not draw. A holistic
judge can also score a turn highly whose NLI consistency is low; recall and
a holistic judge both miss that.

Protocol (the autoregressive multi-turn setting):
  turn t: write [assistant_{t-1}, user_t] via memory.set_from_messages into
          a FRESH namespace per dialogue; ask loom the user turn via
          memory.ask; the answer IS assistant_t for every later turn.
  grade:  each answer against the dialogue so far with an entailment judge
          (bench/rubrics/entailment.md → {label, p_entail,
          p_contradict, quality, contradicted_span}).
          Consistency Score  CS  = (1 + P_ent − P_con) / 2
          Dialogue Entailment Rate DER = share of turns labelled entailed
  report: per-turn rows CSV, a per-turn-index curve (n, mean CS, DER, mean
          quality), and with --compare a paired, dialogue-clustered bootstrap
          of the CS / DER / quality deltas between two runs.

The judge is the seam: the rubric asks for the two NLI probabilities a
DeBERTa-style cross-encoder emits natively, so `--judge` can later point at
a local NLI model without changing a column. For now it is any
OpenAI-compatible chat endpoint (OPENAI_API_KEY / OPENAI_BASE_URL, as
bench/longmemeval.py) or `--judge none`.

Hard subset: ceiling and floor cases mask component
deltas — a dialogue every arm gets right or every arm gets wrong carries no
information, and averaging over them is how an A/B reads "neutral".
`--hard-subset --baseline prior.csv` keeps dialogues whose baseline quality
sits in a mid band, ranks them by locally computed complexity (words/turn,
type-token ratio, unique entities, new-entity rate, conditionals, negative
constraints) and keeps the top fraction; it prints the original-vs-refined
stats table so the selection is auditable.

Usage:
    python bench/dialogue_consistency.py --dry-run                       # fixture, no network
    python bench/dialogue_consistency.py --base-url $LOOM_BASE_URL --token $TOK \\
        --dataset smoke --judge gpt-4o-mini --out bench/dialogue_consistency.csv
    python bench/dialogue_consistency.py --dataset longmemeval:/path/longmemeval_s_cleaned.json \\
        --limit 40 --max-turns 6 --base-url ... --token ... --out bench/dc_lme.csv
    python bench/dialogue_consistency.py --compare bench/dc_a.csv bench/dc_b.csv
    python bench/dialogue_consistency.py --dataset smoke --hard-subset \\
        --baseline bench/dc_a.csv --dry-run

What this does NOT measure: recall (bench/quality.py), answer correctness
against a gold (bench/longmemeval.py). A turn can be entailed by the
dialogue and still be wrong about the world; that is a different bench.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import statistics
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bench.bench_namespaces import (
    erase_on_exit_sync,
    register_bench_namespace_sync,
    remember_bench_namespace,
)
from bench.bootstrap import BootResult, paired_bootstrap

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dialogue_consistency_smoke.json"
RUBRIC = Path(__file__).resolve().parent / "rubrics" / "entailment.md"

_OPENAI_CHAT_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip(
    "/"
) + "/chat/completions"

LABELS = ("entailed", "neutral", "contradicted")

CSV_COLUMNS = [
    "run_id",
    "dialogue_id",
    "task_type",
    "turn_index",
    "n_turns",
    "question",
    "answer",
    "cited_keys",
    "latency_ms",
    "source",
    "route_tier",
    "abstain",
    "label",
    "p_entail",
    "p_contradict",
    "cs",
    "quality",
    "contradicted_span",
    "rationale",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dialogue:
    dialogue_id: str
    task_type: str
    turns: list[str]  # user turns, in order; each is also the question loom is asked


@dataclass
class TurnRow:
    run_id: str
    dialogue_id: str
    task_type: str
    turn_index: int
    n_turns: int
    question: str
    answer: str
    cited_keys: str
    latency_ms: int
    source: str
    route_tier: str
    abstain: bool
    label: str = "neutral"
    p_entail: float = 0.0
    p_contradict: float = 0.0
    cs: float = 0.5
    quality: float = 0.0
    contradicted_span: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class Verdict:
    label: str
    p_entail: float
    p_contradict: float
    quality: float
    contradicted_span: str = ""
    rationale: str = ""


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def consistency_score(p_entail: float, p_contradict: float) -> float:
    """CS = (1 + P_ent − P_con) / 2, clamped to [0, 1]. 1.0 = entailed,
    0.0 = contradicted, 0.5 = the judge is neutral or undecided."""
    v = (1.0 + float(p_entail) - float(p_contradict)) / 2.0
    return max(0.0, min(1.0, v))


def entailment_rate(labels: Iterable[str]) -> float:
    ls = list(labels)
    if not ls:
        return 0.0
    return sum(1 for x in ls if x == "entailed") / len(ls)


def turn_curve(rows: Sequence[TurnRow]) -> list[dict[str, Any]]:
    """Per turn index: n, mean CS, DER, mean quality — the Figure-4 shape."""
    by_turn: dict[int, list[TurnRow]] = defaultdict(list)
    for r in rows:
        by_turn[int(r.turn_index)].append(r)
    out: list[dict[str, Any]] = []
    for t in sorted(by_turn):
        rs = by_turn[t]
        out.append(
            {
                "turn_index": t,
                "n": len(rs),
                "mean_cs": round(statistics.fmean(r.cs for r in rs), 4),
                "der": round(entailment_rate(r.label for r in rs), 4),
                "mean_quality": round(statistics.fmean(r.quality for r in rs), 4),
                "contradicted": sum(1 for r in rs if r.label == "contradicted"),
            }
        )
    return out


def summarize(rows: Sequence[TurnRow]) -> dict[str, Any]:
    if not rows:
        return {"n_turns": 0, "n_dialogues": 0, "cs": 0.0, "der": 0.0, "quality": 0.0, "curve": []}
    return {
        "n_turns": len(rows),
        "n_dialogues": len({r.dialogue_id for r in rows}),
        "cs": round(statistics.fmean(r.cs for r in rows), 4),
        "der": round(entailment_rate(r.label for r in rows), 4),
        "quality": round(statistics.fmean(r.quality for r in rows), 4),
        "abstain": sum(1 for r in rows if r.abstain),
        "latency_ms_p50": int(statistics.median(r.latency_ms for r in rows)),
        "curve": turn_curve(rows),
    }


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_verdict(text: str) -> Verdict:
    """Tolerant of prose and code fences around the JSON; anything
    unparseable is a neutral 0.5 verdict, never an exception — a judge
    hiccup must not read as a contradiction."""
    m = _JSON_RE.search(text or "")
    if not m:
        return Verdict("neutral", 0.0, 0.0, 0.0, "", "unparseable judge reply")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Verdict("neutral", 0.0, 0.0, 0.0, "", "unparseable judge reply")

    def _p(k: str) -> float:
        try:
            return max(0.0, min(1.0, float(obj.get(k, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    pe, pc = _p("p_entail"), _p("p_contradict")
    label = str(obj.get("label", "")).strip().lower()
    if label not in LABELS:
        # Derive from the probabilities when the label is missing or odd.
        label = (
            "entailed" if pe >= 0.5 and pe > pc else ("contradicted" if pc >= 0.5 else "neutral")
        )
    return Verdict(
        label=label,
        p_entail=pe,
        p_contradict=pc,
        quality=_p("quality"),
        contradicted_span=str(obj.get("contradicted_span", "") or "")[:300],
        rationale=str(obj.get("rationale", "") or "")[:300],
    )


def render_premise(transcript: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"[{role}] {text}" for role, text in transcript)


def judge_entailment(
    client: httpx.Client,
    *,
    model: str,
    api_key: str,
    premise: str,
    hypothesis: str,
    rubric: str,
) -> Verdict:
    """One judge call: premise = the dialogue so far, hypothesis = loom's
    answer. Temperature 0. Returns a neutral verdict on transport errors."""
    user = (
        f"### Premise (dialogue so far)\n{premise}\n\n"
        f"### Hypothesis (the new answer)\n{hypothesis}\n"
    )
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 300,
        "messages": [{"role": "system", "content": rubric}, {"role": "user", "content": user}],
    }
    try:
        r = client.post(
            _OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=60.0,
        )
        r.raise_for_status()
        reply = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        return Verdict("neutral", 0.0, 0.0, 0.0, "", f"judge error: {e}")
    return parse_verdict(reply)


_NEG_RE = re.compile(
    r"\b(not|no|never|cannot|can't|isn't|aren't|wasn't|didn't|doesn't|don't)\b", re.I
)


def heuristic_verdict(premise: str, hypothesis: str) -> Verdict:
    """The --dry-run / --judge none judge. Lexical only: a hypothesis whose
    content words all occur in the premise is 'entailed'; one that negates a
    premise sentence it otherwise repeats is 'contradicted'; else neutral.
    It exists so the plumbing (CSV, curve, compare, hard subset) runs with
    no network; its numbers mean nothing about loom."""
    p_words = set(re.findall(r"[a-z0-9]+", premise.lower()))
    h_words = [w for w in re.findall(r"[a-z0-9]+", hypothesis.lower()) if len(w) > 3]
    if not h_words:
        return Verdict("neutral", 0.3, 0.1, 0.2, "", "empty hypothesis")
    covered = sum(1 for w in h_words if w in p_words) / len(h_words)
    neg_h, neg_p = bool(_NEG_RE.search(hypothesis)), bool(_NEG_RE.search(premise))
    if covered >= 0.8 and neg_h != neg_p:
        return Verdict("contradicted", 0.1, 0.8, 0.3, hypothesis[:80], "negation flip")
    if covered >= 0.6:
        return Verdict("entailed", 0.5 + covered / 2, 0.05, 0.6, "", "lexically covered")
    return Verdict("neutral", 0.3, 0.2, 0.4, "", "partially covered")


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


def load_fixture(path: Path = FIXTURE) -> list[Dialogue]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Dialogue(
            dialogue_id=str(d["dialogue_id"]),
            task_type=str(d.get("task_type", "")),
            turns=[str(t) for t in d["turns"]],
        )
        for d in data["dialogues"]
    ]


def longmemeval_to_dialogues(
    items: Sequence[dict[str, Any]], *, max_turns: int = 6
) -> list[Dialogue]:
    """Reshape LongMemEval-S: the evidence sessions' USER turns (in order,
    capped) become the dialogue, and the question is the final turn. The
    assistant side is deliberately dropped — under the autoregressive
    protocol loom writes its own assistant turns. Items with no evidence
    session are skipped."""
    out: list[Dialogue] = []
    for it in items:
        ids = {str(s) for s in it.get("answer_session_ids", [])}
        sess_ids = [str(s) for s in it.get("haystack_session_ids", [])]
        sessions = it.get("haystack_sessions", [])
        users: list[str] = []
        for sid, sess in zip(sess_ids, sessions, strict=False):
            if sid not in ids:
                continue
            for m in sess:
                if str(m.get("role")) == "user" and str(m.get("content", "")).strip():
                    users.append(str(m["content"]).strip())
        if not users:
            continue
        turns = users[: max(1, max_turns - 1)] + [str(it["question"])]
        out.append(
            Dialogue(
                dialogue_id=str(it["question_id"]),
                task_type=str(it.get("question_type", "longmemeval")),
                turns=turns,
            )
        )
    return out


def load_dataset(spec: str, *, max_turns: int, limit: int) -> list[Dialogue]:
    if spec in ("smoke", "fixture", ""):
        ds = load_fixture()
    elif spec.startswith("longmemeval:"):
        path = Path(spec.split(":", 1)[1])
        ds = longmemeval_to_dialogues(
            json.loads(path.read_text(encoding="utf-8")), max_turns=max_turns
        )
    elif spec.startswith("json:"):
        ds = load_fixture(Path(spec.split(":", 1)[1]))
    else:
        raise SystemExit(
            f"unknown --dataset {spec!r}: use smoke | longmemeval:<path> | json:<path>"
        )
    ds = [Dialogue(d.dialogue_id, d.task_type, d.turns[:max_turns]) for d in ds]
    return ds[:limit] if limit > 0 else ds


# ---------------------------------------------------------------------------
# Hard subset
# ---------------------------------------------------------------------------

_COND_RE = re.compile(
    r"\b(if|unless|only if|provided that|as long as|otherwise|when(ever)?)\b", re.I
)
_NEGC_RE = re.compile(
    r"\b(not|never|cannot|can't|no longer|must not|mustn't|shouldn't|without|except|neither|nor)\b",
    re.I,
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]+|\d+(?:[.,]\d+)?")
_ENTITY_RE = re.compile(
    r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z0-9'-]+(?:\s+[A-Z][a-zA-Z0-9'-]+)*)\b|\b(\d+(?:[.,]\d+)?)\b"
)


def _entities(text: str) -> set[str]:
    """Cheap, LLM-free: capitalised runs not at a sentence start, plus
    numbers. Good enough to RANK dialogues by how much there is to track;
    not an extractor."""
    ents: set[str] = set()
    for sent in re.split(r"(?<=[.!?])\s+", text):
        words = sent.split()
        for i, w in enumerate(words):
            core = w.strip(".,;:!?()\"'")
            if not core:
                continue
            if re.fullmatch(r"\d+(?:[.,]\d+)?", core):
                ents.add(core)
            elif i > 0 and core[0].isupper() and core.lower() not in ("i", "i'm", "i've"):
                ents.add(core.lower())
    return ents


@dataclass(frozen=True)
class Complexity:
    avg_words_per_turn: float
    ttr: float
    unique_entities: int
    new_entity_rate: float
    conditional_freq: float
    negative_constraint_freq: float


def complexity(d: Dialogue) -> Complexity:
    words_per_turn = [len(_TOKEN_RE.findall(t)) for t in d.turns]
    all_tokens = [w.lower() for t in d.turns for w in _TOKEN_RE.findall(t)]
    seen: set[str] = set()
    new_per_turn: list[int] = []
    for t in d.turns:
        e = _entities(t)
        new_per_turn.append(len(e - seen))
        seen |= e
    n = max(1, len(d.turns))
    return Complexity(
        avg_words_per_turn=round(statistics.fmean(words_per_turn) if words_per_turn else 0.0, 2),
        ttr=round(len(set(all_tokens)) / max(1, len(all_tokens)), 4),
        unique_entities=len(seen),
        new_entity_rate=round(sum(new_per_turn) / n, 3),
        conditional_freq=round(sum(1 for t in d.turns if _COND_RE.search(t)) / n, 3),
        negative_constraint_freq=round(sum(1 for t in d.turns if _NEGC_RE.search(t)) / n, 3),
    )


def _rank_scores(values: Sequence[float]) -> list[float]:
    """Average rank in [0, 1] (ties share), so features on different scales
    add without one dominating."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def baseline_quality(rows_csv: Path) -> dict[str, float]:
    """Mean judge quality per dialogue from a prior run's rows CSV."""
    acc: dict[str, list[float]] = defaultdict(list)
    with rows_csv.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                acc[r["dialogue_id"]].append(float(r["quality"]))
            except (KeyError, ValueError):
                continue
    return {k: statistics.fmean(v) for k, v in acc.items() if v}


def hard_subset(
    dialogues: Sequence[Dialogue],
    *,
    baseline: dict[str, float] | None,
    band: tuple[float, float] = (0.4, 0.8),
    fraction: float = 0.25,
    min_keep: int = 1,
) -> tuple[list[Dialogue], dict[str, Any]]:
    """Stage 1 — drop ceiling/floor dialogues by baseline quality (skipped
    when no baseline is given). Stage 2 — rank the rest by the summed rank
    of six complexity features, keep the top `fraction`. Returns the
    subset and an auditable stats table (original vs refined means)."""
    stage1 = list(dialogues)
    if baseline:
        lo, hi = band
        stage1 = [d for d in dialogues if lo <= baseline.get(d.dialogue_id, -1.0) <= hi]
    feats = {d.dialogue_id: complexity(d) for d in stage1}
    names = list(Complexity.__dataclass_fields__)
    cols = {nm: [float(getattr(feats[d.dialogue_id], nm)) for d in stage1] for nm in names}
    ranks = {nm: _rank_scores(cols[nm]) for nm in names}
    score = {d.dialogue_id: sum(ranks[nm][i] for nm in names) for i, d in enumerate(stage1)}
    ordered = sorted(stage1, key=lambda d: (-score[d.dialogue_id], d.dialogue_id))
    keep = max(min_keep, int(round(len(ordered) * fraction))) if ordered else 0
    refined = ordered[:keep]

    def _means(ds: Sequence[Dialogue]) -> dict[str, float]:
        if not ds:
            return {nm: 0.0 for nm in names} | {"avg_turns": 0.0}
        cx = [complexity(d) for d in ds]
        out = {nm: round(statistics.fmean(float(getattr(c, nm)) for c in cx), 3) for nm in names}
        out["avg_turns"] = round(statistics.fmean(len(d.turns) for d in ds), 2)
        return out

    stats = {
        "n_original": len(dialogues),
        "n_after_band": len(stage1),
        "n_refined": len(refined),
        "band": list(band) if baseline else None,
        "fraction": fraction,
        "original": _means(dialogues),
        "refined": _means(refined),
        "ranking": [
            {"dialogue_id": d.dialogue_id, "score": round(score[d.dialogue_id], 3)} for d in ordered
        ],
    }
    return refined, stats


def print_stats_table(stats: dict[str, Any]) -> None:
    print(
        f"hard subset: {stats['n_original']} original → {stats['n_after_band']} in band "
        f"{stats['band']} → {stats['n_refined']} refined (top {stats['fraction']:.0%})"
    )
    print(f"{'metric':<28}{'original':>12}{'refined':>12}")
    for k in stats["original"]:
        print(f"{k:<28}{stats['original'][k]:>12}{stats['refined'][k]:>12}")


# ---------------------------------------------------------------------------
# Loom client (HTTP) + a network-free fake for --dry-run
# ---------------------------------------------------------------------------


class LoomClient:
    def __init__(self, base_url: str, token: str, org: str, agent: str = "dc-bench") -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.org = org
        self.agent = agent
        self.http = httpx.Client(timeout=90.0)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        r = self.http.post(self.base_url + path, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    def mint(self, namespace: str) -> None:
        """Register a run's namespace as bench and remember it for the exit erasure."""
        remember_bench_namespace(self.org, namespace)
        register_bench_namespace_sync(
            self._post, org=self.org, namespace=namespace, description="dialogue consistency"
        )

    def write(
        self, namespace: str, messages: list[dict[str, str]], last: list[dict[str, str]]
    ) -> None:
        self._post(
            "/v1/memory.set_from_messages",
            {
                "org": self.org,
                "namespace": namespace,
                "agent": self.agent,
                "messages": messages,
                "last_messages": last[-20:],
                "max_tokens": 2048,
            },
        )

    def ask(self, namespace: str, query: str) -> dict[str, Any]:
        return self._post(
            "/v1/memory.ask",
            {
                "org": self.org,
                "namespace": namespace,
                "agent": self.agent,
                "query": query[:2000],
                "namespace_scope": "this",
            },
        )


class FakeLoom:
    """Answers by restating the most recent user turn — a memory that only
    ever remembers the last thing said, which is exactly the decay shape
    the curve should expose. No network."""

    def __init__(self) -> None:
        self.turns: dict[str, list[str]] = defaultdict(list)

    def mint(self, namespace: str) -> None:
        pass

    def write(
        self, namespace: str, messages: list[dict[str, str]], last: list[dict[str, str]]
    ) -> None:
        for m in messages:
            if m["role"] == "user":
                self.turns[namespace].append(m["content"])

    def ask(self, namespace: str, query: str) -> dict[str, Any]:
        prior = self.turns[namespace][:-1]
        text = (prior[-1] if prior else query)[:200]
        return {
            "answer": f"Based on what you said: {text}",
            "memory_keys": [f"k{len(prior)}"],
            "source": "fake",
            "route_tier": "",
            "abstain": False,
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_dialogue(
    loom: Any,
    d: Dialogue,
    *,
    run_id: str,
    namespace_prefix: str,
    judge: Any,
) -> list[TurnRow]:
    """Autoregressive: loom's own answer is the assistant turn from then on."""
    # The run suffix survives the length cap, so two runs never share a namespace.
    head = f"{namespace_prefix}-{d.dialogue_id}"[:53]
    ns = f"{head}-{run_id[:6]}"
    loom.mint(ns)
    transcript: list[tuple[str, str]] = []
    rows: list[TurnRow] = []
    prev_answer: str | None = None
    for t, user in enumerate(d.turns):
        messages = ([{"role": "assistant", "content": prev_answer}] if prev_answer else []) + [
            {"role": "user", "content": user}
        ]
        last = [{"role": r, "content": c} for r, c in transcript]
        loom.write(ns, messages, last)
        transcript.append(("user", user))
        t0 = time.perf_counter()
        resp = loom.ask(ns, user)
        latency = int((time.perf_counter() - t0) * 1000)
        answer = str(resp.get("answer") or "")
        premise = render_premise(transcript)
        v: Verdict = judge(premise, answer)
        rows.append(
            TurnRow(
                run_id=run_id,
                dialogue_id=d.dialogue_id,
                task_type=d.task_type,
                turn_index=t,
                n_turns=len(d.turns),
                question=user,
                answer=answer,
                cited_keys="|".join(str(k) for k in resp.get("memory_keys", [])),
                latency_ms=latency,
                source=str(resp.get("source", "")),
                route_tier=str(resp.get("route_tier", "")),
                abstain=bool(resp.get("abstain", False)),
                label=v.label,
                p_entail=round(v.p_entail, 4),
                p_contradict=round(v.p_contradict, 4),
                cs=round(consistency_score(v.p_entail, v.p_contradict), 4),
                quality=round(v.quality, 4),
                contradicted_span=v.contradicted_span,
                rationale=v.rationale,
            )
        )
        transcript.append(("assistant", answer))
        prev_answer = answer
    return rows


def write_rows(path: Path, rows: Sequence[TurnRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def read_rows(path: Path) -> list[TurnRow]:
    out: list[TurnRow] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append(
                TurnRow(
                    run_id=r["run_id"],
                    dialogue_id=r["dialogue_id"],
                    task_type=r["task_type"],
                    turn_index=int(r["turn_index"]),
                    n_turns=int(r["n_turns"]),
                    question=r["question"],
                    answer=r["answer"],
                    cited_keys=r["cited_keys"],
                    latency_ms=int(float(r["latency_ms"] or 0)),
                    source=r["source"],
                    route_tier=r["route_tier"],
                    abstain=str(r["abstain"]).lower() == "true",
                    label=r["label"],
                    p_entail=float(r["p_entail"] or 0),
                    p_contradict=float(r["p_contradict"] or 0),
                    cs=float(r["cs"] or 0),
                    quality=float(r["quality"] or 0),
                    contradicted_span=r.get("contradicted_span", ""),
                    rationale=r.get("rationale", ""),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Compare (paired, dialogue-clustered bootstrap; B minus A)
# ---------------------------------------------------------------------------


def compare_runs(
    a: Sequence[TurnRow], b: Sequence[TurnRow], *, seed: int = 7
) -> dict[str, BootResult]:
    key = lambda r: (r.dialogue_id, r.turn_index)  # noqa: E731
    ia = {key(r): r for r in a}
    ib = {key(r): r for r in b}
    common = sorted(set(ia) & set(ib))
    clusters = [k[0] for k in common]
    out: dict[str, BootResult] = {}
    for metric, fn in (
        ("cs", lambda r: r.cs),
        ("der", lambda r: 1.0 if r.label == "entailed" else 0.0),
        ("quality", lambda r: r.quality),
    ):
        deltas = [fn(ib[k]) - fn(ia[k]) for k in common]
        out[metric] = paired_bootstrap(deltas, clusters, seed=seed)
    return out


def print_compare(res: dict[str, BootResult]) -> None:
    print(
        f"{'metric':<10}{'n':>6}{'clusters':>10}{'mean Δ':>10}{'95% lb':>10}{'95% ub':>10}"
        f"{'wins':>6}{'ties':>6}{'losses':>8}"
    )
    for m, r in res.items():
        print(
            f"{m:<10}{r.n:>6}{r.clusters:>10}{r.mean:>+10.4f}{r.lb:>+10.4f}{r.ub:>+10.4f}"
            f"{r.wins:>6}{r.ties:>6}{r.losses:>8}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _make_judge(args: argparse.Namespace) -> Any:
    if args.dry_run or args.judge == "none":
        return heuristic_verdict
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("--judge needs OPENAI_API_KEY (or use --judge none / --dry-run)")
    rubric = Path(args.rubric).read_text(encoding="utf-8")
    client = httpx.Client()

    def _judge(premise: str, hypothesis: str) -> Verdict:
        return judge_entailment(
            client,
            model=args.judge,
            api_key=api_key,
            premise=premise,
            hypothesis=hypothesis,
            rubric=rubric,
        )

    return _judge


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dataset", default="smoke", help="smoke | longmemeval:<path> | json:<path>")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-turns", type=int, default=6)
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--org", default="dev")
    p.add_argument("--namespace-prefix", default="bench-dc")
    p.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="do not erase this run's per-dialogue namespaces when it ends",
    )
    p.add_argument(
        "--judge",
        default=os.environ.get("LOOM_DC_JUDGE_MODEL", "gpt-4o-mini"),
        help="model id or 'none'",
    )
    p.add_argument("--rubric", default=str(RUBRIC))
    p.add_argument("--out", default="bench/dialogue_consistency.csv")
    p.add_argument("--curve-out", default="", help="write the per-turn curve + summary as JSON")
    p.add_argument("--dry-run", action="store_true", help="fake loom + heuristic judge; no network")
    p.add_argument("--hard-subset", action="store_true")
    p.add_argument(
        "--baseline", default="", help="rows CSV of a prior run (per-dialogue quality for the band)"
    )
    p.add_argument("--hard-band", nargs=2, type=float, default=(0.4, 0.8), metavar=("LO", "HI"))
    p.add_argument("--hard-fraction", type=float, default=0.25)
    p.add_argument("--compare", nargs=2, metavar=("A_CSV", "B_CSV"))
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args(argv)

    if args.compare:
        res = compare_runs(
            read_rows(Path(args.compare[0])), read_rows(Path(args.compare[1])), seed=args.seed
        )
        print_compare(res)
        return 0

    dialogues = load_dataset(args.dataset, max_turns=args.max_turns, limit=args.limit)
    if args.hard_subset:
        base = baseline_quality(Path(args.baseline)) if args.baseline else None
        if base is None:
            print(
                "hard subset: no --baseline given; skipping the quality band, "
                "ranking by complexity only"
            )
        dialogues, stats = hard_subset(
            dialogues, baseline=base, band=tuple(args.hard_band), fraction=args.hard_fraction
        )
        print_stats_table(stats)

    loom: Any = FakeLoom() if args.dry_run else LoomClient(args.base_url, args.token, args.org)
    judge = _make_judge(args)
    run_id = uuid.uuid4().hex[:12]
    rows: list[TurnRow] = []
    # The run's namespaces are erased when it exits, after a failure or Ctrl-C too.
    cleanup = (
        contextlib.nullcontext()
        if args.dry_run
        else erase_on_exit_sync(loom._post, org=args.org, keep=args.keep_namespaces)
    )
    with cleanup:
        for d in dialogues:
            rows.extend(
                run_dialogue(
                    loom, d, run_id=run_id, namespace_prefix=args.namespace_prefix, judge=judge
                )
            )
            print(f"  {d.dialogue_id:<28} {d.task_type:<24} turns={len(d.turns)}", file=sys.stderr)

    write_rows(Path(args.out), rows)
    summary = summarize(rows)
    print(json.dumps({k: v for k, v in summary.items() if k != "curve"}, indent=2))
    print(f"{'turn':>5}{'n':>6}{'mean CS':>10}{'DER':>8}{'quality':>10}{'contra':>8}")
    for c in summary["curve"]:
        print(
            f"{c['turn_index']:>5}{c['n']:>6}{c['mean_cs']:>10.3f}{c['der']:>8.3f}{c['mean_quality']:>10.3f}{c['contradicted']:>8}"
        )
    if args.curve_out:
        Path(args.curve_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"rows → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
