"""Code-intelligence QA bench — the rollout gate for the code plane.

Measures four metric families over an authored question set, each gated by
its own --min-* flag (non-zero exit on breach):

  structural exactness   exact-match@1 on symbol definitions, caller-set F1
                         (deterministic — no LLM, no judge)
  retrieval              recall@5 / recall@10 / MRR on conceptual questions
  hub                    hub recall@5: the prefix's most-called symbols
                         whose gold is a path set
  abstention             precision/recall on abstain probes (questions about
                         code NOT in the repo — the correct answer is
                         abstain=true); recall = probes abstained on /
                         probes, precision = probes abstained on / all
                         abstains, so an abstain on an answerable question
                         counts against precision
  token cost             mean/p90 tokens_returned per answered question

Dataset: JSONL rows (see bench/fixtures/code_intel_qa.jsonl):
  {"id", "type": symbol_def|callers|conceptual|hub|history|test_set|abstain_probe,
   "query", "gold": {...}}  (+ optional lane, source, facets, namespace;
   other keys, e.g. "notes", are ignored)
gold by type:
  symbol_def    {"qualified_name": "...", "path_prefix": "src/..."}
  callers       {"symbol": "...", "expected_callers": ["QName", ...]}
  conceptual    {"paths": ["src/...", ...]}   (recall = share of gold path
                                              prefixes in the top k)
  hub           {"path_prefix": "src/...", "symbols": [top-5 qualified names]}
  abstain_probe {}                            (must abstain)

House conventions: standalone argparse+httpx; pure scoring core above the
live-path banner (unit-tested by bench/test_code_intel_qa.py, CH-free);
--dry self-asserts that a perfect synthetic retrieval scores each lane's
ceiling, so CI exercises the scorer without a server. Metric hygiene:
recall@k and judged accuracy are different axes — the gates are judge-free
(structural gold is exact); --judge adds an LLM answer check, gated only
with --min-judge-accept.

Each summary names the index revision that served the answers
(as_of.commit per code.search answer, and the stale count) and the callers
edges per tier (scip = compiler, treesitter = name-resolved).

PROVENANCE of the question files (bench/fixtures/):
  code_intel_qa, _multilang, _sqlsh   hand written over ClickHouse/ClickHouse
                     (Apache-2.0 source); gold names paths and symbols only
  code_intel_mined_issue, _mined_pr_title, _history
                     mined by bench/code_intel_mine_prs.py from the code-
                     clickhouse PR ledger (the lane split and the pr_title
                     sample are recorded in each file's header)
  code_intel_mined_issue_ka   bench/code_intel_ka_slice.py over the two above
  code_intel_hub     a census of live `calls` edges (method in its header)
  code_intel_oss     imported from an MIT-licensed annotation corpus (upstream
                     URL, revision and LLM-authored-gold caveat in its header)
  code_intel_qa_langfuse   bench/gen_code_intel_qa_langfuse.py over a langfuse
                     (MIT) checkout; the file records no checkout sha, so
                     regenerating it on another revision changes the gold
  code_calibration_probes (not a question file; read by code.calibrate)
                     hand written: ClickHouse chunk excerpts paired with the
                     question each answers and a near-miss distractor
All of them name public repositories only; a sensitive-data scan finds no
tokens, internal hosts or personal emails.

Run:
  python bench/code_intel_qa.py --dry
  python bench/code_intel_qa.py --base-url http://localhost:7777 \
      --min-symbol-exact 0.95 --min-callers-f1 0.85 --min-recall5 0.75 \
      --min-abstain-precision 0.8 --max-mean-tokens 4000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "code_intel_qa.jsonl"

# Explicit (org, namespace) merged into every loom payload. Without these
# the transport authz checks the token's GLOBAL roles (the body carries no
# namespace to scope against), so a namespace-scoped deploy token 403s on
# all 60 questions. Empty values are omitted — the server then falls back
# to LOOM_CODE_ORG/LOOM_CODE_NAMESPACE, which only works for global tokens.
_IDENTITY: dict[str, str] = {}


def _identity_for(q: Question) -> dict[str, str]:
    """The (org, namespace) a question's payload carries: the run's identity,
    with the row's own `namespace` on top when the fixture names one — a
    multi-repository lane (code_intel_oss) asks each question
    against the namespace its repository was indexed under."""
    return {**_IDENTITY, **({"namespace": q.namespace} if q.namespace else {})}


# ---------------------------------------------------------------------------
# Pure scoring core. CH-free, LLM-free, unit-tested by
# bench/test_code_intel_qa.py. Everything below the LIVE PATH banner depends
# on a live server; everything here is deterministic given retrievals.
# ---------------------------------------------------------------------------


@dataclass
class Question:
    qid: str
    qtype: str  # symbol_def | callers | conceptual | abstain_probe | hub | history
    query: str
    gold: dict
    # Provenance of a mined question ({pr_number, issue, merged_at}); a
    # merged_at pins the history leg to PRs older than the fix (pr_leg_before).
    source: dict = field(default_factory=dict)
    # Fixture lane (issue / pr_title / number_issue / number_pr / phrase_pr /
    # phrase_issue); `--lane` filters on it.
    lane: str = ""
    # The namespace this row is asked against when the fixture spans several
    # repositories (code-oss-<name>); empty = the run's --namespace.
    namespace: str = ""
    # A `test_set` question may carry the facets that name its
    # answer exactly ([{kind, name}]); the retrieval then goes to
    # code.tests_for instead of code.search, scored the same way.
    facets: list[dict] = field(default_factory=list)
    # A `callers` question may restrict the walk to edge kinds
    # (`["inherits"]` for the implementors lane); empty = every graph kind.
    edge_kinds: list[str] = field(default_factory=list)


@dataclass
class Retrieval:
    """What the server returned for one question, normalized."""

    top_symbol: str = ""  # qualified name of definitions[0] (symbol_def)
    callers: list[str] = field(default_factory=list)  # qualified names
    paths: list[str] = field(default_factory=list)  # ranked result paths
    symbols: list[str] = field(default_factory=list)  # ranked result symbols (hub)
    abstained: bool = False
    tokens_returned: int = 0
    error: str = ""
    # code.search retrieval_work counters (lexical_queries, same_path_in_topk,
    # saturation_moved, ...); the summary reports the share of questions with
    # each counter > 0 so a ranking step's REACH is measured next to its effect.
    work: dict[str, int] = field(default_factory=dict)
    # Which leg found each ranked unit (aligned with paths;
    # empty on servers that predate it) and the call's wall time
    via: list[str] = field(default_factory=list)
    # unit kind per ranked result (function / class_skeleton / file_header /
    # other), aligned with paths; a reranker's kind preference reads off it
    kinds: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    # The served route and the PR numbers of the history
    # section (empty when the intent did not fire or the server predates it)
    route: str = ""
    history_prs: list[int] = field(default_factory=list)
    # Index revision the answer was served from (code.search `as_of.commit`) and
    # whether the server called that index stale; "" / False when not reported
    index_commit: str = ""
    index_stale: bool = False
    # callers answers: edges per tier (scip = compiler-exact, treesitter =
    # name-resolved, overlay = a PR's own call sites)
    edge_tiers: dict[str, int] = field(default_factory=dict)


# --no-facets: score `test_set` rows through code.search even when they carry
# facets — the retrieval control for the facet read.
_NO_FACETS = False


def _work_counters(raw: Any) -> dict[str, int]:
    """`retrieval_work` as integer counters. The server adds keys over time and not
    every value is a count (a string-valued key can appear); a non-integer value is
    skipped rather than failing the question — a bench must not error on a field it does
    not read."""
    out: dict[str, int] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, (bool, int, float)):
            out[str(k)] = int(v)
        elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
            out[str(k)] = int(v)
    return out


def load_questions(path: Path) -> list[Question]:
    out: list[Question] = []
    seen: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        row = json.loads(line)
        q = Question(
            qid=str(row["id"]),
            qtype=str(row["type"]),
            query=str(row["query"]),
            gold=dict(row.get("gold") or {}),
            source=dict(row.get("source") or {}),
            lane=str(row.get("lane") or ""),
            namespace=str(row.get("namespace") or ""),
            facets=list(row.get("facets") or []),
            edge_kinds=list(row.get("edge_kinds") or []),
        )
        if q.qid in seen:
            raise ValueError(f"duplicate question id: {q.qid}")
        seen.add(q.qid)
        if q.qtype not in {
            "symbol_def",
            "callers",
            "conceptual",
            "abstain_probe",
            "hub",
            "test_set",
            "history",
        }:
            raise ValueError(f"{q.qid}: unknown type {q.qtype}")
        if q.qtype == "history" and not (q.gold.get("pr_number") and q.gold.get("paths")):
            raise ValueError(f"{q.qid}: history needs gold.pr_number and gold.paths")
        # referential integrity: gold must carry what the scorer needs
        if q.qtype == "symbol_def" and not q.gold.get("qualified_name"):
            raise ValueError(f"{q.qid}: symbol_def needs gold.qualified_name")
        if q.qtype == "callers" and not q.gold.get("expected_callers"):
            raise ValueError(f"{q.qid}: callers needs gold.expected_callers")
        if q.qtype in {"conceptual", "test_set"} and not q.gold.get("paths"):
            raise ValueError(f"{q.qid}: conceptual needs gold.paths")
        if q.qtype == "hub" and not (q.gold.get("path_prefix") and q.gold.get("symbols")):
            raise ValueError(f"{q.qid}: hub needs gold.path_prefix and gold.symbols")
        out.append(q)
    return out


def hit_at_k(gold_paths: list[str], got_paths: list[str], k: int) -> float:
    """1.0 when ANY gold path is in the top-k (the `test_set` lane: the gold is the
    whole set of tests carrying a literal — 2 to 40 files — so set recall@5 has a
    ceiling of 5/|gold| and says nothing about whether the ranking found the set at
    all)."""
    return 1.0 if recall_at_k(gold_paths, got_paths, k) > 0 else 0.0


def precision_at_k(gold_paths: list[str], got_paths: list[str], k: int) -> float:
    """Share of the top-k results that are gold (path-prefix rule as
    `recall_at_k`); the `test_set` lane's second number — a facet read that
    names the set exactly scores 1.0 here and in hit@5."""
    top = [p for p in got_paths[:k] if p]
    if not top:
        return 0.0
    golds = [g.rstrip("/") for g in gold_paths]
    hits = sum(
        1
        for p in top
        if any(
            p == g or p.startswith(g + "/") or p.startswith(g + ".") or g.startswith(p)
            for g in golds
        )
    )
    return hits / len(top)


def _aliases(gold: dict) -> dict[str, str]:
    """{other spelling: canonical} for the gold names: the two tiers can name ONE
    definition differently — Go's parser tier is path-scoped (`a.b.util::Recv.Name`),
    its compiler tier package-scoped (`example.com/a/b::Recv::Name`) — and the read
    collapses them onto the compiler row, so a gold row may list the spellings it
    accepts."""
    out: dict[str, str] = {}
    for alias in gold.get("aliases") or []:
        out[str(alias)] = str(gold.get("qualified_name") or gold.get("symbol") or "")
    for canonical, alts in (gold.get("caller_aliases") or {}).items():
        for alias in alts:
            out[str(alias)] = str(canonical)
    return out


def score_symbol_def(q: Question, r: Retrieval) -> float:
    """1.0 when the top definition IS the gold symbol (path prefix optional);
    a gold `aliases` list names the other tier's spelling of the same row."""
    if r.abstained or not r.top_symbol:
        return 0.0
    if _aliases(q.gold).get(r.top_symbol, r.top_symbol) != q.gold["qualified_name"]:
        return 0.0
    prefix = q.gold.get("path_prefix")
    if prefix and r.paths and not r.paths[0].startswith(prefix):
        return 0.0
    return 1.0


def score_callers_f1(q: Question, r: Retrieval) -> float:
    expected = set(q.gold["expected_callers"])
    aliases = _aliases(q.gold)
    got = {aliases.get(c, c) for c in r.callers}
    if not got:
        return 0.0
    tp = len(expected & got)
    precision = tp / len(got)
    recall = tp / len(expected) if expected else 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def score_hub_recall5(q: Question, r: Retrieval) -> float:
    """Share of the gold top-5 qualified names among the first five result
    symbols. Gold is the materialised truth (the same aggregation the weekly
    pass writes), so this measures the read path — prefix filter, test and
    external exclusion, dependency order — not ranking."""
    gold = [str(s) for s in q.gold["symbols"]][:5]
    if not gold or r.abstained and not r.symbols:
        return 0.0
    got = set(r.symbols[:5])
    return sum(1 for s in gold if s in got) / len(gold)


def score_history_pr_hit(q: Question, r: Retrieval) -> float:
    """1.0 when the gold PR is in the served history section."""
    return 1.0 if int(q.gold["pr_number"]) in set(r.history_prs) else 0.0


def history_fired(r: Retrieval) -> bool:
    return r.route.startswith("history_")


def recall_at_k(gold_paths: list[str], got_paths: list[str], k: int) -> float:
    """Fraction of gold paths present in the top-k results. A gold entry is
    a PATH PREFIX: "src/Access" (directory), "src/Access/EnabledQuota"
    (mechanism — matches the .h and the .cpp), or a full path. Extension-
    pinned golds systematically mis-scored correct header retrievals."""
    if not gold_paths:
        return 0.0
    top = got_paths[:k]
    hit = 0
    for g in gold_paths:
        g = g.rstrip("/")
        if any(
            p == g or p.startswith(g + "/") or p.startswith(g + ".") or g.startswith(p) for p in top
        ):
            hit += 1
    return hit / len(gold_paths)


def _moved_back(path: str, moved: dict[str, str]) -> str:
    """A path under the prefix it had at the gold's commit (`gold.moved`: current
    prefix -> gold prefix), for a file that moved after the gold was written."""
    for now, then in moved.items():
        if path.startswith(now):
            return then + path[len(now) :]
    return path


def mrr(gold_paths: list[str], got_paths: list[str]) -> float:
    for rank, p in enumerate(got_paths, start=1):
        for g in gold_paths:
            g = g.rstrip("/")
            if p == g or p.startswith(g + "/") or p.startswith(g + ".") or g.startswith(p):
                return 1.0 / rank
    return 0.0


@dataclass
class Summary:
    n: int = 0
    errors: int = 0
    symbol_exact: float = 0.0
    callers_f1: float = 0.0
    recall5: float = 0.0
    recall10: float = 0.0
    mrr: float = 0.0
    # Any gold path OR audited alternate in the top k (1/0 per
    # question). recall@k above is the set fraction and stays the series'
    # gate; a two-path gold found once scores 0.5 there and 1.0 here.
    hit5: float = 0.0
    hit10: float = 0.0
    # Matched-budget placebo control: per question, the same NUMBER of paths
    # drawn (seeded, without replacement) from the pool of paths the system
    # returned across ALL conceptual questions. If real recall doesn't beat
    # this, the recall figure is mostly pool coverage, not ranking — a small
    # gold universe plus a broad answer budget can make random look good.
    placebo_recall5: float = 0.0
    placebo_recall10: float = 0.0
    abstain_precision: float = 0.0
    abstain_recall: float = 0.0
    hub_recall5: float = 0.0
    # The `test_set` lane — hit@5 (any gold test in the top 5),
    # precision@5 (share of the top 5 that are gold), set recall@10
    test_hit5: float = 0.0
    test_p5: float = 0.0
    test_r10: float = 0.0
    # History lane: gold PR in the served section; gold files
    # in the top 5 (the same recall as the conceptual lane, over history
    # questions); share of history questions on which the intent fired.
    history_pr_hit: float = 0.0
    history_recall5: float = 0.0
    history_fired: float = 0.0
    # share of ALL code.search answers (every lane) served by a history_*
    # route — the guard fixtures must read ~0 here
    history_fire_rate: float = 0.0
    mean_tokens: float = 0.0
    p90_tokens: int = 0
    by_type: dict[str, dict[str, float]] = field(default_factory=dict)
    # Provenance of the top-5 units over conceptual answers
    # (lexical / vector / both / symbol) and code.search p50
    via_top5: dict[str, int] = field(default_factory=dict)
    p50_latency_ms: float = 0.0
    # share of conceptual questions whose retrieval_work counter is > 0
    work_share: dict[str, float] = field(default_factory=dict)
    # excluded errors by kind ("ReadTimeout", "HTTPStatusError", ...)
    error_kinds: dict[str, int] = field(default_factory=dict)
    # Conceptual recall cut by fixture lane, source.category and
    # source.language — {"lane": {"oss": {n, recall@5, recall@10, mrr}}, ...};
    # a facet appears only when the fixture carries it on some row
    by_facet: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    # Provenance: answers per served index commit, answers the server flagged
    # stale, and callers edges per tier — which index and which tier a run measured
    index_commits: dict[str, int] = field(default_factory=dict)
    stale_answers: int = 0
    edge_tiers: dict[str, int] = field(default_factory=dict)


def _provenance(s: Summary, pairs: list[tuple[Question, Retrieval]]) -> None:
    for _q, r in pairs:
        if r.error:
            continue
        if r.index_commit:
            key = r.index_commit[:12]
            s.index_commits[key] = s.index_commits.get(key, 0) + 1
        s.stale_answers += int(r.index_stale)
        for tier, n in r.edge_tiers.items():
            s.edge_tiers[tier] = s.edge_tiers.get(tier, 0) + n


def summarize(pairs: list[tuple[Question, Retrieval]]) -> Summary:
    s = Summary(n=len(pairs))
    sym_scores: list[float] = []
    f1_scores: list[float] = []
    r5s: list[float] = []
    h5s: list[float] = []
    h10s: list[float] = []
    th5: list[float] = []
    tp5: list[float] = []
    tr10: list[float] = []
    r10s: list[float] = []
    mrrs: list[float] = []
    hubs: list[float] = []
    hist_hits: list[float] = []
    hist_r5s: list[float] = []
    hist_fired: list[float] = []
    fired_any: list[float] = []
    conceptual_rows: list[tuple[list[str], list[str]]] = []
    facet_rows: dict[str, dict[str, list[tuple[float, float, float]]]] = {}
    work_hits: dict[str, int] = {}
    tokens: list[int] = []
    # abstention confusion: probe questions SHOULD abstain
    probe_abstained = probe_total = 0
    answerable_abstained = 0
    abstain_count = 0
    via_counts: dict[str, int] = {}
    latencies: list[float] = []

    for q, r in pairs:
        if r.error:
            s.errors += 1
            kind = r.error.split(":", 1)[0].strip() or "error"
            s.error_kinds[kind] = s.error_kinds.get(kind, 0) + 1
            continue  # errors are excluded from ratios, never counted as misses
        if r.abstained:
            abstain_count += 1
        if q.qtype == "abstain_probe":
            probe_total += 1
            if r.abstained:
                probe_abstained += 1
            continue
        if r.abstained:
            answerable_abstained += 1
        tokens.append(r.tokens_returned)
        if q.qtype in {"conceptual", "history"}:
            fired_any.append(1.0 if history_fired(r) else 0.0)
        if q.qtype == "symbol_def":
            sym_scores.append(score_symbol_def(q, r))
        elif q.qtype == "callers":
            f1_scores.append(score_callers_f1(q, r))
        elif q.qtype == "test_set":
            gold = list(q.gold["paths"])
            th5.append(hit_at_k(gold, r.paths, 5))
            tp5.append(precision_at_k(gold, r.paths, 5))
            tr10.append(recall_at_k(gold, r.paths, 10))
        elif q.qtype == "conceptual":
            gold = list(q.gold["paths"])
            got = [_moved_back(p, q.gold.get("moved") or {}) for p in r.paths]
            r5s.append(recall_at_k(gold, got, 5))
            r10s.append(recall_at_k(gold, got, 10))
            mrrs.append(mrr(gold, got))
            any_gold = gold + list(q.gold.get("alternates") or [])
            h5s.append(hit_at_k(any_gold, got, 5))
            h10s.append(hit_at_k(any_gold, got, 10))
            conceptual_rows.append((gold, got))
            for facet, value in _facets_of(q):
                facet_rows.setdefault(facet, {}).setdefault(value, []).append(
                    (r5s[-1], r10s[-1], mrrs[-1])
                )
            for v in r.via[:5]:
                via_counts[v or "?"] = via_counts.get(v or "?", 0) + 1
            if r.latency_ms > 0:
                latencies.append(r.latency_ms)
            for key, v in r.work.items():
                work_hits[key] = work_hits.get(key, 0) + (1 if v > 0 else 0)
        elif q.qtype == "hub":
            hubs.append(score_hub_recall5(q, r))
        elif q.qtype == "history":
            hist_hits.append(score_history_pr_hit(q, r))
            hist_r5s.append(recall_at_k(list(q.gold["paths"]), r.paths, 5))
            hist_fired.append(1.0 if history_fired(r) else 0.0)
            if r.latency_ms > 0:
                latencies.append(r.latency_ms)

    def avg(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    s.symbol_exact = avg(sym_scores)
    s.callers_f1 = avg(f1_scores)
    s.recall5 = avg(r5s)
    s.recall10 = avg(r10s)
    s.mrr = avg(mrrs)
    s.hit5 = avg(h5s)
    s.hit10 = avg(h10s)
    s.hub_recall5 = avg(hubs)
    s.test_hit5 = avg(th5)
    s.test_p5 = avg(tp5)
    s.test_r10 = avg(tr10)
    s.history_pr_hit = avg(hist_hits)
    s.history_recall5 = avg(hist_r5s)
    s.history_fired = avg(hist_fired)
    s.history_fire_rate = avg(fired_any)

    # Placebo arm: shuffled-pool ranking at matched budget. The pool is
    # sorted + the RNG is seeded, so the control is reproducible and cannot
    # be gamed by run order. A tiny pool (few questions) makes the placebo
    # trivially strong — that reads as a small-instrument warning, not a
    # ranking failure.
    if conceptual_rows:
        import random as _random

        pool = sorted({p for _, paths in conceptual_rows for p in paths})
        rng = _random.Random(1729)
        p5s, p10s = [], []
        for gold, paths in conceptual_rows:
            k = min(len(paths) if paths else 10, len(pool))
            sample = rng.sample(pool, k) if k else []
            p5s.append(recall_at_k(gold, sample, 5))
            p10s.append(recall_at_k(gold, sample, 10))
        s.placebo_recall5 = avg(p5s)
        s.placebo_recall10 = avg(p10s)
    s.abstain_recall = probe_abstained / probe_total if probe_total else 0.0
    # precision: of everything that abstained, how much SHOULD have
    s.abstain_precision = probe_abstained / abstain_count if abstain_count else 1.0
    s.mean_tokens = avg([float(t) for t in tokens])
    s.work_share = {k: round(v / len(r5s), 3) for k, v in sorted(work_hits.items())} if r5s else {}
    s.p90_tokens = sorted(tokens)[int(0.9 * (len(tokens) - 1))] if tokens else 0
    s.via_top5 = dict(sorted(via_counts.items(), key=lambda kv: -kv[1]))
    s.by_facet = {
        facet: {
            value: {
                "n": len(rows),
                "recall@5": round(avg([x[0] for x in rows]), 3),
                "recall@10": round(avg([x[1] for x in rows]), 3),
                "mrr": round(avg([x[2] for x in rows]), 3),
            }
            for value, rows in sorted(values.items())
        }
        for facet, values in sorted(facet_rows.items())
    }
    s.p50_latency_ms = sorted(latencies)[len(latencies) // 2] if latencies else 0.0
    _provenance(s, pairs)
    s.by_type = {
        "symbol_def": {"n": len(sym_scores), "exact@1": round(s.symbol_exact, 3)},
        "callers": {"n": len(f1_scores), "f1": round(s.callers_f1, 3)},
        "history": {
            "n": len(hist_hits),
            "pr_hit": round(s.history_pr_hit, 3),
            "recall@5": round(s.history_recall5, 3),
            "fired": round(s.history_fired, 3),
        },
        "conceptual": {
            "n": len(r5s),
            "recall@5": round(s.recall5, 3),
            "recall@10": round(s.recall10, 3),
            "mrr": round(s.mrr, 3),
            "hit@5": round(s.hit5, 3),
            "hit@10": round(s.hit10, 3),
            "placebo@5": round(s.placebo_recall5, 3),
            "placebo@10": round(s.placebo_recall10, 3),
        },
        "abstain_probe": {
            "n": probe_total,
            "recall": round(s.abstain_recall, 3),
            "precision": round(s.abstain_precision, 3),
            "wrong_abstains_on_answerable": answerable_abstained,
        },
        "hub": {"n": len(hubs), "recall@5": round(s.hub_recall5, 3)},
        "test_set": {
            "n": len(th5),
            "hit@5": round(s.test_hit5, 3),
            "precision@5": round(s.test_p5, 3),
            "recall@10": round(s.test_r10, 3),
        },
    }
    return s


def _facets_of(q: Question) -> list[tuple[str, str]]:
    """(facet, value) pairs a conceptual row carries: its lane, and the
    corpus category / language of its source when the fixture records them."""
    out: list[tuple[str, str]] = []
    if q.lane:
        out.append(("lane", q.lane))
    for key in ("category", "language"):
        if q.source.get(key):
            out.append((key, str(q.source[key])))
    return out


def build_synthetic_retrieval(q: Question) -> Retrieval:
    """--dry: fabricate the perfect retrieval so the scorer self-asserts."""
    if q.qtype == "symbol_def":
        return Retrieval(
            top_symbol=q.gold["qualified_name"],
            paths=[q.gold.get("path_prefix", "src/") + "x.cpp"],
            tokens_returned=800,
        )
    if q.qtype == "callers":
        return Retrieval(callers=list(q.gold["expected_callers"]), tokens_returned=300)
    if q.qtype in {"conceptual", "test_set"}:
        return Retrieval(paths=list(q.gold["paths"]), tokens_returned=2000)
    if q.qtype == "hub":
        return Retrieval(symbols=list(q.gold["symbols"]), tokens_returned=400)
    if q.qtype == "history":
        return Retrieval(
            paths=list(q.gold["paths"]),
            route="history_number",
            history_prs=[int(q.gold["pr_number"])],
            tokens_returned=400,
        )
    return Retrieval(abstained=True)


def dump_pairs(
    path: str,
    arm: str,
    pairs: list[tuple[Question, Retrieval]],
) -> None:
    """One JSON line per question for one arm; append so arms A and B (and
    lanes run in separate processes) land in the same file."""
    with open(path, "a", encoding="utf-8") as fh:
        for q, r in pairs:
            fh.write(
                json.dumps(
                    {
                        "arm": arm,
                        "qid": q.qid,
                        "lane": q.lane,
                        "type": q.qtype,
                        "query": q.query,
                        "gold": q.gold,
                        "paths": r.paths,
                        "via": r.via,
                        "callers": r.callers,
                        "kinds": r.kinds,
                        "abstained": r.abstained,
                        "error": r.error,
                        "latency_ms": round(r.latency_ms, 1),
                        "route": r.route,
                        "work": r.work,
                        "tokens_returned": r.tokens_returned,
                        "index_commit": r.index_commit,
                        "index_stale": r.index_stale,
                        "edge_tiers": r.edge_tiers,
                    }
                )
                + "\n"
            )


def compare_arms(
    pairs_a: list[tuple[Question, Retrieval]],
    pairs_b: list[tuple[Question, Retrieval]],
) -> dict[str, Any]:
    """Paired conceptual recall@5, arm B minus arm A, per question, with a
    clustered bootstrap over the gold subsystem (first two path segments).
    Pure, so the gate's number is testable without a server."""
    try:  # bench/ on sys.path (script) or imported as the bench package (tests)
        from bootstrap import cluster_of_path, paired_bootstrap
    except ImportError:
        from bench.bootstrap import cluster_of_path, paired_bootstrap

    by_qid_b = {q.qid: r for q, r in pairs_b}
    deltas: list[float] = []
    clusters: list[str] = []
    moved: list[tuple[str, float]] = []
    a_scores: list[float] = []
    b_scores: list[float] = []
    # Secondary paired metrics next to recall@5: recall@10 and MRR, same pairing, same clusters.
    extra: dict[str, tuple[list[float], list[float]]] = {
        "recall@10": ([], []),
        "mrr": ([], []),
    }
    lex_ms: tuple[list[float], list[float]] = ([], [])
    # The history lane pairs on recall@5 like the conceptual
    # lane and adds PR hit (gold PR in the served section) as a secondary
    pr_hit: tuple[list[float], list[float]] = ([], [])
    pr_clusters: list[str] = []
    errored_b = 0
    for q, ra in pairs_a:
        if q.qtype not in {"conceptual", "history"} or q.qid not in by_qid_b:
            continue
        rb = by_qid_b[q.qid]
        errored_b += 1 if rb.error else 0
        if ra.error or rb.error:
            continue
        gold = list(q.gold.get("paths") or [])
        if q.qtype == "history":
            pr_hit[0].append(score_history_pr_hit(q, ra))
            pr_hit[1].append(score_history_pr_hit(q, rb))
            pr_clusters.append(_cluster_for(q, gold, cluster_of_path))
        sa = recall_at_k(gold, ra.paths, 5)
        sb = recall_at_k(gold, rb.paths, 5)
        a_scores.append(sa)
        b_scores.append(sb)
        deltas.append(sb - sa)
        clusters.append(_cluster_for(q, gold, cluster_of_path))
        if abs(sb - sa) > 1e-9:
            moved.append((q.qid, sb - sa))
        extra["recall@10"][0].append(recall_at_k(gold, ra.paths, 10))
        extra["recall@10"][1].append(recall_at_k(gold, rb.paths, 10))
        extra["mrr"][0].append(mrr(gold, ra.paths))
        extra["mrr"][1].append(mrr(gold, rb.paths))
        for side, r in ((0, ra), (1, rb)):
            if "lexical_leg_ms" in r.work:
                lex_ms[side].append(float(r.work["lexical_leg_ms"]))
    boot = paired_bootstrap(deltas, clusters)
    moved.sort(key=lambda t: t[1])
    secondary: dict[str, dict[str, Any]] = {}
    if pr_hit[0]:
        secondary["history pr_hit"] = {
            "a": sum(pr_hit[0]) / len(pr_hit[0]),
            "b": sum(pr_hit[1]) / len(pr_hit[1]),
            "boot": paired_bootstrap(
                [vb - va for va, vb in zip(pr_hit[0], pr_hit[1], strict=True)], pr_clusters
            ),
        }
    for name, (xa, xb) in extra.items():
        secondary[name] = {
            "a": sum(xa) / len(xa) if xa else 0.0,
            "b": sum(xb) / len(xb) if xb else 0.0,
            "boot": paired_bootstrap([vb - va for va, vb in zip(xa, xb, strict=True)], clusters),
        }
    return {
        "n": len(deltas),
        "errored_b": errored_b,
        "recall5_a": sum(a_scores) / len(a_scores) if a_scores else 0.0,
        "recall5_b": sum(b_scores) / len(b_scores) if b_scores else 0.0,
        "boot": boot,
        "moved": moved,
        "secondary": secondary,
        "lexical_leg_ms_p50": (_p50(lex_ms[0]), _p50(lex_ms[1])),
    }


def _cluster_for(q: Question, gold: list[str], cluster_of_path: Any) -> str:
    """Bootstrap cluster of a paired question: the gold subsystem (first two
    path segments), prefixed with the row's namespace on a multi-repository
    fixture so `src/x` in two repositories is two clusters."""
    if not gold:
        return q.qid
    key = str(cluster_of_path(gold[0]))
    return f"{q.namespace}:{key}" if q.namespace else key


def _p50(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


_PLACEBO_MIN_QUESTIONS = 10


def gate(s: Summary, args: argparse.Namespace) -> tuple[int, list[str]]:
    problems: list[str] = []
    checks = [
        ("symbol exact@1", s.symbol_exact, args.min_symbol_exact, True),
        ("callers F1", s.callers_f1, args.min_callers_f1, True),
        ("conceptual recall@5", s.recall5, args.min_recall5, True),
        ("hub recall@5", s.hub_recall5, getattr(args, "min_hub_recall5", None), True),
        ("test_set hit@5", s.test_hit5, getattr(args, "min_test_hit5", None), True),
        ("abstain precision", s.abstain_precision, args.min_abstain_precision, True),
        ("abstain recall", s.abstain_recall, args.min_abstain_recall, True),
    ]
    for name, got, want, is_min in checks:
        if want is not None and ((got < want) if is_min else (got > want)):
            problems.append(f"{name} {got:.3f} < required {want:.3f}")
    # Pre-committed placebo rule: a gated recall claim must beat the
    # matched-budget shuffled-pool control, or the number is mostly pool
    # coverage and no recall gate may pass on it.
    # A tiny conceptual pool makes the shuffled control trivially strong (one
    # question's own paths ARE the pool), so the rule reads as an instrument
    # warning below _PLACEBO_MIN_QUESTIONS and as a gate at or above it.
    conceptual_n = int(s.by_type.get("conceptual", {}).get("n", 0))
    if (
        args.min_recall5 is not None
        and conceptual_n >= _PLACEBO_MIN_QUESTIONS
        and s.recall5 <= s.placebo_recall5
    ):
        problems.append(
            f"conceptual recall@5 {s.recall5:.3f} does not beat the "
            f"matched-budget placebo {s.placebo_recall5:.3f} — the recall "
            "figure is mostly answer-pool coverage, not ranking"
        )
    if args.max_mean_tokens is not None and s.mean_tokens > args.max_mean_tokens:
        problems.append(f"mean tokens {s.mean_tokens:.0f} > allowed {args.max_mean_tokens}")
    # Errored questions are excluded from ratios (a flaky question must not
    # read as a miss), but they make the RUN untrustworthy as a gate: with
    # every question errored (an expired token) each ratio sits at its
    # empty-denominator default. Fail loudly.
    if s.errors:
        problems.append(
            f"{s.errors} question(s) errored — metrics incomplete, gate refused "
            "(check auth/token expiry and server health, then rerun)"
        )
    return (1 if problems else 0), problems


# ---------------------------------------------------------------------------
# LIVE PATH — everything below talks to a running loom over HTTP.
# ---------------------------------------------------------------------------


async def run_live(
    questions: list[Question],
    base_url: str,
    token: str,
    rerank: str = "",
    search_extra: dict[str, Any] | None = None,
    token_budget: int | None = None,
) -> list[tuple[Question, Retrieval]]:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    pairs: list[tuple[Question, Retrieval]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=headers) as client:
        for q in questions:
            try:
                pairs.append((q, await _ask(client, q, rerank, token_budget, search_extra)))
            except Exception as e:  # noqa: BLE001 — errors excluded from ratios
                pairs.append((q, Retrieval(error=f"{type(e).__name__}: {e}")))
    return pairs


async def _post(client, path: str, payload: dict[str, Any], *, attempts: int = 24):
    """POST that waits out a 429 instead of failing the question: the
    server's per-namespace token bucket (code ops: 60 burst, 10/s) is
    exactly what a paired bench on a memoised arm trips, and a rate-limited
    question is not an errored one. Sleeps Retry-After (else one second),
    up to `attempts` times, then raises like any other status."""
    import asyncio as _asyncio

    for _ in range(attempts):
        r = await client.post(path, json=payload)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        try:
            wait = float(r.headers.get("retry-after") or 1.0)
        except ValueError:
            wait = 1.0
        try:
            # the bucket's own refill horizon (a 30-burst bench
            # class refills over ~6 s; Retry-After alone under-waits)
            wait = max(wait, float(r.headers.get("x-ratelimit-reset") or 0.0))
        except ValueError:
            pass
        await _asyncio.sleep(min(max(wait, 0.2), 10.0))
    r.raise_for_status()
    return r


async def run_live_paired(
    questions: list[Question],
    base_url: str,
    token: str,
    rerank: str = "",
    search_extra: dict[str, Any] | None = None,
    compare_extra: dict[str, Any] | None = None,
    token_budget: int | None = None,
) -> tuple[list[tuple[Question, Retrieval]], list[tuple[Question, Retrieval]]]:
    """Both arms, one question at a time, A then B back to back. A live namespace
    re-indexes under a long run (in a sequential run the index can move commits between
    the two arms, and the served-response memo can keep replaying arm A from the old
    index while arm B computes on the new one); interleaving puts any drift on both arms
    of the same pair instead of on one arm."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    pairs_a: list[tuple[Question, Retrieval]] = []
    pairs_b: list[tuple[Question, Retrieval]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=headers) as client:
        for q in questions:
            for extra, out in ((search_extra, pairs_a), (compare_extra, pairs_b)):
                try:
                    out.append((q, await _ask(client, q, rerank, token_budget, extra)))
                except Exception as e:  # noqa: BLE001 — errors excluded from ratios
                    out.append((q, Retrieval(error=f"{type(e).__name__}: {e}")))
    return pairs_a, pairs_b


async def _ask(
    client,
    q: Question,
    rerank: str = "",
    token_budget: int | None = None,
    search_extra: dict[str, Any] | None = None,
) -> Retrieval:
    if q.qtype == "callers":
        body: dict[str, Any] = {**_identity_for(q), "name": q.gold["symbol"], "depth": 1}
        if q.edge_kinds:
            body["edge_kinds"] = q.edge_kinds
        r = await _post(client, "/v1/code.callers", body)
        data = r.json()
        tiers: dict[str, int] = {}
        for e in data["edges"]:
            tiers[str(e.get("tier") or "?")] = tiers.get(str(e.get("tier") or "?"), 0) + 1
        return Retrieval(
            callers=sorted({e["from_symbol"] for e in data["edges"]}), edge_tiers=tiers
        )
    if q.qtype in ("test_set", "abstain_probe") and q.facets and not _NO_FACETS:
        # The facets name the answer; one keyed read of the statement records instead of
        # a ranked search. An abstain probe that names a facet ("TypeScript that reads
        # the ClickHouse table <absent>") asks the same op — the honest answer is the
        # per-kind `not_covered` / `no_match`, either of which is an abstain for a name
        # the tree never carries — not a ranked text search whose generic words
        # ("reads", "table") always find something
        t0 = time.perf_counter()
        try:
            r = await _post(  # waits out the bench class's 429s
                client,
                "/v1/code.tests_for",
                {**_identity_for(q), "facets": q.facets, "limit": 50, "consumer": "bench"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            # a server without the op: the question falls through
            # to code.search, scored the same way
            return await _ask_search(client, q, rerank, token_budget, search_extra)
        data = r.json()
        return Retrieval(
            paths=[str(t["path"]) for t in data.get("tests") or []],
            # `no_match` is an answer for a test_set question (the records are
            # complete for the kind) and, having returned no test, an abstain
            # for a probe — the bench's "the plane produced no false answer"
            abstained=bool(data.get("abstain"))
            or (q.qtype == "abstain_probe" and not (data.get("tests") or [])),
            tokens_returned=int(data.get("tokens_returned") or 0),
            route=str(data.get("verdict") or "facets"),
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
    if q.qtype == "hub":
        # the prefix's most-called symbols: sort_by='fan_in' under the gold
        # prefix (arm A of the hub lane runs the same call without the sort,
        # via --search-extra / --compare-extra)
        r = await _post(
            client,
            "/v1/code.search",
            {
                **_identity_for(q),
                "query": q.query,
                "top_k": 10,
                "consumer": "bench",
                "path_prefix": q.gold["path_prefix"],
                "bodies": "none",
                "fresh": True,
                **(search_extra or {}),
            },
        )
        data = r.json()
        return Retrieval(
            symbols=[str(u.get("symbol") or "") for u in data.get("results") or []],
            paths=[str(u["path"]) for u in data.get("results") or []],
            abstained=bool(data.get("abstain")),
            tokens_returned=int(data.get("tokens_returned") or 0),
        )
    if q.qtype == "symbol_def":
        r = await _post(client, "/v1/code.symbol", {**_identity_for(q), "name": q.query})
        data = r.json()
        defs = data.get("definitions") or []
        return Retrieval(
            top_symbol=str(defs[0]["symbol"]) if defs else "",
            paths=[str(d["path"]) for d in defs],
            tokens_returned=int(data.get("tokens_returned") or 0),
        )
    if q.qtype == "history":
        # a question ABOUT a change: never pinned — the
        # question asks for the PR itself; the section's PR numbers and the
        # served route come back beside the ranked paths
        import time as _time

        t0 = _time.perf_counter()
        r = await _post(
            client,
            "/v1/code.search",
            {
                **_identity_for(q),
                "query": q.query,
                "top_k": 10,
                "consumer": "bench",
                **({"token_budget": token_budget} if token_budget else {}),
                "rerank": rerank,
                **(search_extra or {}),
            },
        )
        data = r.json()
        results = data.get("results") or []
        section = data.get("history") or {}
        return Retrieval(
            paths=[str(u["path"]) for u in results],
            abstained=bool(data.get("abstain")),
            tokens_returned=int(data.get("tokens_returned") or 0),
            work=_work_counters(data.get("retrieval_work")),
            via=[str(u.get("via") or "") for u in results],
            latency_ms=(_time.perf_counter() - t0) * 1000.0,
            route=str(data.get("route") or ""),
            history_prs=[int(p.get("pr_number") or 0) for p in (section.get("prs") or [])],
            **_as_of(data),
        )
    return await _ask_search(client, q, rerank, token_budget, search_extra)


async def _ask_search(
    client,
    q: Question,
    rerank: str = "",
    token_budget: int | None = None,
    search_extra: dict[str, Any] | None = None,
) -> Retrieval:
    """conceptual, test_set (without facets, or `--no-facets`) and
    abstain_probe go through code.search."""
    import time as _time

    t0 = _time.perf_counter()
    # A question mined from a merged PR pins the history leg to PRs merged
    # before its fix: the fix must not vote for its own files. Inert when the
    # leg is off, so every arm sends it.
    pinned = (
        {"pr_leg_before": str(q.source["merged_at"]).replace(" ", "T")}
        if q.source.get("merged_at")
        else {}
    )
    r = await _post(
        client,
        "/v1/code.search",
        {
            **_identity_for(q),
            "query": q.query,
            "top_k": 10,
            "consumer": "bench",
            **({"token_budget": token_budget} if token_budget else {}),
            "rerank": rerank,
            **pinned,
            **(search_extra or {}),
        },
    )
    data = r.json()
    results = data.get("results") or []
    return Retrieval(
        paths=[str(u["path"]) for u in results],
        abstained=bool(data.get("abstain")),
        tokens_returned=int(data.get("tokens_returned") or 0),
        work=_work_counters(data.get("retrieval_work")),
        via=[str(u.get("via") or "") for u in results],
        kinds=[str(u.get("kind") or "") for u in results],
        latency_ms=(_time.perf_counter() - t0) * 1000.0,
        route=str(data.get("route") or ""),
        history_prs=[
            int(p.get("pr_number") or 0) for p in ((data.get("history") or {}).get("prs") or [])
        ],
        **_as_of(data),
    )


def _as_of(data: dict[str, Any]) -> dict[str, Any]:
    """The served index revision off a code.search response's `as_of` block."""
    as_of = data.get("as_of") or {}
    return {"index_commit": str(as_of.get("commit") or ""), "index_stale": bool(as_of.get("stale"))}


async def track_misses(
    pairs: list[tuple[Question, Retrieval]],
    base_url: str,
    token: str,
    search_extra: dict[str, Any] | None = None,
    token_budget: int | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """For every conceptual miss (recall@5 < 1), ask the server where the
    FIRST gold path stood in this call's leg pools (CodeSearchInput.track_path).
    Returns (qid, gold_path, tracked) — the verdict reads
    'served at rank r' / 'rank r in <leg> pool' / 'no lexeme match' /
    'beyond pool: ...' / 'not indexed', so a miss is attributed to a leg, to
    fusion, or to the index, not guessed."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    out: list[tuple[str, str, dict[str, Any]]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=headers) as client:
        for q, r in pairs:
            if q.qtype != "conceptual" or r.error:
                continue
            gold = list(q.gold.get("paths") or [])
            if not gold or recall_at_k(gold, r.paths, 5) >= 1.0:
                continue
            missing = [g for g in gold if not any(p.startswith(g) for p in r.paths[:5])]
            target = missing[0] if missing else gold[0]
            try:
                resp = await client.post(
                    "/v1/code.search",
                    json={
                        **_identity_for(q),
                        "query": q.query,
                        "top_k": 10,
                        "consumer": "bench",
                        "track_path": target,
                        "fresh": True,
                        **({"token_budget": token_budget} if token_budget else {}),
                        **(search_extra or {}),
                    },
                )
                resp.raise_for_status()
                out.append((q.qid, target, dict(resp.json().get("tracked") or {})))
            except Exception as e:  # noqa: BLE001 — a diagnostic never fails the bench
                out.append((q.qid, target, {"verdict": f"track call failed: {e}"}))
    return out


_JUDGE_MODEL = "gpt-4o-2024-08-06"  # pinned — judge drift invalidates trend lines
_JUDGE_SYSTEM = (
    "You grade an AI assistant's answer to a question about the ClickHouse "
    "repository — its C++ engine AND its own python/shell test and CI "
    "tooling (pytest integration framework, clickhouse-test, docker "
    "helpers, praktika CI). Answers about test/CI mechanisms that cite "
    "python/shell files are fully in scope, not off-topic. The answer "
    "should explain the actual mechanism and cite "
    "source files. You are given hint paths where the mechanism lives "
    "(prefixes; related files also count). Grade ACCEPT if the answer is "
    "grounded in cited code, describes the mechanism plausibly and "
    "consistently with the hint locations, and does not fabricate. Grade "
    "REJECT if it is vague, ungrounded, cites unrelated code as the core "
    "mechanism, or invents details. First line: exactly ACCEPT or REJECT. "
    "Then two sentences of reasoning. (Verdict-then-rationale: one-word "
    "judges measured 2x harsher than the same judge with reasoning.)"
)


def dump_verdicts(path: Path, verdicts: list[dict[str, Any]], *, run: int) -> None:
    """One JSON line per judged question: what a paired (per-question) read
    of two arms needs, which the run mean cannot give."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for v in verdicts:
            fh.write(
                json.dumps(
                    {
                        "qid": v.get("qid"),
                        "run": run,
                        "verdict": v.get("verdict"),
                        "reason": v.get("reason", ""),
                        "context_tokens": v.get("context_tokens"),
                        "source": v.get("source", ""),
                        "detail": v.get("detail", ""),
                        # the answer and its citations, so a dump can be re-judged
                        # offline (a new hint rule, a new judge) without re-synthesis
                        "answer": v.get("answer", ""),
                        "citations": v.get("citations", ""),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def parse_verdict(text: str) -> tuple[str, str]:
    """The judge's verdict and its rationale. The verdict is the first word;
    the rationale is everything after it — on the same line ("REJECT. The
    answer …", which the judge writes as often as not) or on the following
    lines; a line-split rule leaves most rejects with an empty rationale."""
    raw = (text or "").strip()
    first, _, rest = raw.partition("\n")
    head = first.strip().lstrip("*# ").upper()
    verdict = "accept" if head.startswith("ACCEPT") else "reject"
    same_line = first.strip().lstrip("*# ")
    for tok in ("ACCEPT", "REJECT"):
        if same_line.upper().startswith(tok):
            same_line = same_line[len(tok) :].lstrip(".:*- ")
            break
    reason = " ".join(x for x in (same_line, " ".join(rest.split())) if x).strip()
    return verdict, reason[:300]


async def judge_answers(
    questions: list[Question],
    base_url: str,
    token: str,
    token_budget: int | None = None,
) -> dict[str, Any]:
    """End-to-end answer lane: conceptual questions -> /v1/code.ask ->
    pinned binary judge. An abstained answer to an answerable question
    counts as a reject (the question set only contains in-repo mechanisms).
    Requires OPENAI_API_KEY for the judge call."""
    import httpx

    judge_key = os.environ.get("OPENAI_API_KEY", "")
    if not judge_key:
        raise SystemExit("--judge requires OPENAI_API_KEY for the judge model")
    conceptual = [q for q in questions if q.qtype == "conceptual"]
    verdicts: list[dict[str, Any]] = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=90.0, headers=headers) as loom,
        httpx.AsyncClient(timeout=60.0) as oa,
    ):
        for q in conceptual:
            row: dict[str, Any] = {"qid": q.qid, "query": q.query}
            try:
                r = await loom.post(
                    "/v1/code.ask",
                    # no_cache: judge CURRENT synthesis quality, not whatever
                    # an earlier run cached (the fresh answer refreshes it)
                    json={
                        **_identity_for(q),
                        "question": q.query,
                        "consumer": "bench",
                        "no_cache": True,
                        **({"token_budget": token_budget} if token_budget else {}),
                    },
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:  # noqa: BLE001
                row.update(verdict="error", detail=f"{type(e).__name__}: {e}")
                verdicts.append(row)
                continue
            row["source"] = data.get("source", "")
            # Retrieval context the synthesis saw (tokens). Paired with the
            # verdict this is one point on the tokens-vs-quality curve.
            row["context_tokens"] = int(data.get("context_tokens") or 0)
            row["answer_chars"] = len((data.get("answer") or "").strip())
            if data.get("abstain") or not (data.get("answer") or "").strip():
                row["verdict"] = "reject_abstained"
                verdicts.append(row)
                continue
            cites = ", ".join(
                f"{c.get('path')}:{c.get('start_line')}" for c in (data.get("citations") or [])[:8]
            )
            # The audited alternates are files a developer
            # would accept as the answer; the judge is told so, or it rejects
            # a correct answer for naming the other route to the mechanism
            hints = list(q.gold.get("paths", [])) + list(q.gold.get("alternates") or [])
            prompt = (
                f"Question: {q.query}\n\n"
                f"Hint paths (mechanism lives here): {hints}\n\n"
                f"Answer:\n{data['answer']}\n\nCited: {cites}"
            )
            row["answer"] = str(data["answer"])[:6000]
            row["citations"] = cites
            jr = await oa.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {judge_key}"},
                json={
                    "model": _JUDGE_MODEL,
                    "temperature": 0,
                    "max_tokens": 200,
                    "messages": [
                        {"role": "system", "content": _JUDGE_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            jr.raise_for_status()
            text = jr.json()["choices"][0]["message"]["content"].strip()
            row["verdict"], row["reason"] = parse_verdict(text)
            verdicts.append(row)
    graded = [v for v in verdicts if v["verdict"] != "error"]
    accepted = sum(1 for v in graded if v["verdict"] == "accept")
    abstained = sum(1 for v in graded if v["verdict"] == "reject_abstained")
    ctx = [v["context_tokens"] for v in graded if v.get("context_tokens")]
    return {
        "n": len(graded),
        "errors": len(verdicts) - len(graded),
        "accepted": accepted,
        "abstained": abstained,
        "accept_rate": round(accepted / len(graded), 3) if graded else 0.0,
        "mean_context_tokens": round(sum(ctx) / len(ctx)) if ctx else 0,
        "mean_answer_chars": round(sum(v.get("answer_chars", 0) for v in graded) / len(graded))
        if graded
        else 0,
        "verdicts": verdicts,
    }


def _print_summary(s: Summary) -> None:
    print("\n=== code_intel_qa ===")
    print(
        f"questions: {s.n}  errors(excluded): {s.errors}"
        + (f"  {s.error_kinds}" if s.error_kinds else "")
    )
    for t, m in s.by_type.items():
        print(f"  {t:14} {m}")
    print(f"tokens/answer: mean {s.mean_tokens:.0f}  p90 {s.p90_tokens}")
    if s.via_top5 or s.p50_latency_ms:
        print(f"via(top5, conceptual): {s.via_top5}  code.search p50 {s.p50_latency_ms:.0f} ms")
    if s.by_type.get("history", {}).get("n"):
        print(
            f"history lane: pr_hit {s.history_pr_hit:.3f}  recall@5 {s.history_recall5:.3f}  "
            f"fired {s.history_fired:.3f}"
        )
    if s.by_type.get("conceptual", {}).get("n") or s.by_type.get("history", {}).get("n"):
        print(f"history_* route fire rate over code.search answers: {s.history_fire_rate:.3f}")
    if s.work_share:
        print(
            "retrieval_work share>0 (conceptual): "
            + "  ".join(f"{k}={v:.3f}" for k, v in s.work_share.items())
        )
    if s.index_commits or s.stale_answers:
        print(
            f"index revision (answers per as_of.commit): {s.index_commits}  stale {s.stale_answers}"
        )
    if s.edge_tiers:
        print(f"callers edges by tier: {dict(sorted(s.edge_tiers.items()))}")
    _print_facets(s)


def _print_facets(s: Summary, indent: str = "") -> None:
    """Conceptual recall by lane / category / language; one
    line per facet value, only when the fixture carries the facet."""
    for facet, values in s.by_facet.items():
        if len(values) < 2 and facet != "lane":
            continue
        for value, m in values.items():
            print(
                f"{indent}  by {facet:8} {value:14} n={m['n']:<4} r@5 {m['recall@5']:.3f}  "
                f"r@10 {m['recall@10']:.3f}  mrr {m['mrr']:.3f}"
            )


def _perfect_recall5(questions: list[Question], qtype: str) -> float:
    """Mean recall@5 of the synthetic retrieval (the gold paths in fixture order).
    It is below 1.0 when a question has more than five gold paths, or when one
    served path matches two gold prefixes and so fewer than five paths fit."""
    rows = [q for q in questions if q.qtype == qtype]
    return sum(recall_at_k(list(q.gold["paths"]), list(q.gold["paths"]), 5) for q in rows) / len(
        rows
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default=str(_FIXTURE))
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default=os.environ.get("LOOM_CODE_ORG", ""))
    ap.add_argument("--namespace", default=os.environ.get("LOOM_CODE_NAMESPACE", ""))
    ap.add_argument("--dry", action="store_true", help="synthetic retrievals, no server")
    ap.add_argument(
        "--lane", default="", help="only questions whose fixture `lane` is one of these (CSV)"
    )
    ap.add_argument(
        "--compare-extra",
        action="append",
        default=[],
        help="JSON search overrides for a SECOND arm; prints the paired "
        "conceptual recall@5 delta (arm B minus arm A) with a "
        "clustered-bootstrap 95%% interval, clusters = gold subsystem. "
        "Repeatable: each value is one arm B against the same arm A.",
    )
    ap.add_argument(
        "--interleave",
        action="store_true",
        help="with --compare-extra: ask arm A and the FIRST arm B back to back per "
        "question, so a namespace re-indexing mid-run moves both arms",
    )
    ap.add_argument(
        "--min-compare-lb",
        type=float,
        default=None,
        help="gate: the paired delta's 95%% lower bound must reach this "
        "(e.g. 0.0 = 'not worse', a positive value = 'better')",
    )
    ap.add_argument(
        "--search-extra",
        default="",
        help="JSON merged into every code.search payload for A/B, e.g. "
        '\'{"kind_prior": "structural"}\'',
    )
    ap.add_argument(
        "--rerank", default="", help="rerank sentinel for code.search (e.g. builtin:openai)"
    )
    ap.add_argument("--verbose", action="store_true", help="per-question scores")
    ap.add_argument(
        "--dump",
        default="",
        help="append one JSON line per (question, arm) with the ranked "
        "paths, via, kinds and wall time, so a paired run can be re-read "
        "per question without re-asking the server",
    )
    ap.add_argument(
        "--track-misses",
        action="store_true",
        help="on every conceptual miss, ask the server where the gold "
        "path stood in each leg's pool (track_path)",
    )
    ap.add_argument("--min-symbol-exact", type=float, default=None)
    ap.add_argument("--min-callers-f1", type=float, default=None)
    ap.add_argument("--min-recall5", type=float, default=None)
    ap.add_argument("--min-abstain-precision", type=float, default=None)
    ap.add_argument("--min-abstain-recall", type=float, default=None)
    ap.add_argument(
        "--min-hub-recall5",
        type=float,
        default=None,
        help="gate: hub lane recall@5",
    )
    ap.add_argument(
        "--min-test-hit5",
        type=float,
        default=None,
        help="gate on the test_set lane's hit@5",
    )
    ap.add_argument(
        "--no-facets",
        action="store_true",
        help="test_set rows go through code.search, not code.tests_for",
    )
    ap.add_argument("--max-mean-tokens", type=float, default=None)
    ap.add_argument(
        "--judge",
        action="store_true",
        help="also run conceptual questions through code.ask and "
        "grade answers with the pinned judge (needs OPENAI_API_KEY)",
    )
    ap.add_argument("--min-judge-accept", type=float, default=None)
    ap.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help="retrieval token budget for code.search and code.ask "
        "(server default when omitted); sweep it to place the "
        "tokens-vs-quality knee",
    )
    ap.add_argument(
        "--judge-dump",
        default=None,
        help="append one JSON line per judged question and run "
        "(qid, run, verdict, reason, context_tokens) so two arms can "
        "be paired per question — a single judged run on n=36 swings +-0.2",
    )
    ap.add_argument(
        "--judge-runs",
        type=int,
        default=1,
        help="repeat the judge lane N times and gate on the MEAN "
        "(single-run variance on n=30 is ~+-0.05)",
    )
    args = ap.parse_args(argv)
    global _NO_FACETS
    _NO_FACETS = bool(getattr(args, "no_facets", False))

    if args.org:
        _IDENTITY["org"] = args.org
    if args.namespace:
        _IDENTITY["namespace"] = args.namespace

    questions = load_questions(Path(args.dataset))
    if args.lane:
        lanes = {x.strip() for x in args.lane.split(",") if x.strip()}
        questions = [q for q in questions if q.lane in lanes]
        if not questions:
            print(f"no questions in lane(s) {sorted(lanes)}", file=sys.stderr)
            return 2
    print(f"loaded {len(questions)} questions from {args.dataset}")

    if args.dry:
        pairs = [(q, build_synthetic_retrieval(q)) for q in questions]
        s = summarize(pairs)
        _print_summary(s)
        # per-type: a single-lane fixture (hub) has no rows of the others
        if s.by_type["symbol_def"]["n"]:
            assert s.symbol_exact == 1.0, s
        if s.by_type["callers"]["n"]:
            assert s.callers_f1 == 1.0, s
        if s.by_type["conceptual"]["n"]:
            assert abs(s.recall5 - _perfect_recall5(questions, "conceptual")) < 1e-9, s
        if s.by_type["abstain_probe"]["n"]:
            assert s.abstain_precision == 1.0 and s.abstain_recall == 1.0, s
        if s.by_type["hub"]["n"]:
            assert s.hub_recall5 == 1.0, s
        if s.by_type["history"]["n"]:
            ceiling = _perfect_recall5(questions, "history")
            assert s.history_pr_hit == 1.0 and abs(s.history_recall5 - ceiling) < 1e-9, s
        assert s.errors == 0
        print("--dry self-check passed (perfect synthetic retrieval scores its ceiling)")
        return 0

    if not args.base_url:
        print("--base-url (or LOOM_BASE_URL) required for a live run", file=sys.stderr)
        return 2
    import asyncio

    pairs_b: list[tuple[Question, Retrieval]] | None = None
    if args.compare_extra and args.interleave:
        pairs, pairs_b = asyncio.run(
            run_live_paired(
                questions,
                args.base_url,
                args.token,
                rerank=args.rerank,
                search_extra=json.loads(args.search_extra) if args.search_extra else None,
                compare_extra=json.loads(args.compare_extra[0]),
                token_budget=args.token_budget,
            )
        )
    else:
        pairs = asyncio.run(
            run_live(
                questions,
                args.base_url,
                args.token,
                rerank=args.rerank,
                search_extra=json.loads(args.search_extra) if args.search_extra else None,
                token_budget=args.token_budget,
            )
        )
    if args.verbose:
        for q, r in pairs:
            if r.error:
                print(f"  {q.qid} ERROR {r.error[:160]}")
            if q.qtype == "conceptual":
                score = recall_at_k(list(q.gold["paths"]), r.paths, 5)
                print(f"  {q.qid} r@5={score:.2f} {'MISS' if score < 1 else 'ok  '} {q.query[:52]}")
                if score < 1:
                    print(f"      got: {[p.split('/')[-1] for p in r.paths[:5]]}")
    s = summarize(pairs)
    _print_summary(s)
    if args.dump:
        dump_pairs(args.dump, "A", pairs)
    if args.track_misses:
        tracked = asyncio.run(
            track_misses(
                pairs,
                args.base_url,
                args.token,
                search_extra=json.loads(args.search_extra) if args.search_extra else None,
                token_budget=args.token_budget,
            )
        )
        print(f"\n=== track_path on {len(tracked)} conceptual misses ===")
        verdict_kinds: dict[str, int] = {}
        for qid, target, t in tracked:
            verdict = str(t.get("verdict") or "?")
            kind = (
                verdict.split(":")[0].split(" in ")[-1]
                if verdict.startswith("rank ")
                else (verdict.split(";")[0].split(":")[0].split(" at ")[0])
            )
            verdict_kinds[kind] = verdict_kinds.get(kind, 0) + 1
            print(f"  {qid} {target.split('/')[-1][:32]:32} {verdict}")
        print("  by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(verdict_kinds.items())))
    rc, problems = gate(s, args)
    for i, arm in enumerate(args.compare_extra):
        if i > 0 or pairs_b is None:  # the interleaved run already produced arm 0
            pairs_b = asyncio.run(
                run_live(
                    questions,
                    args.base_url,
                    args.token,
                    rerank=args.rerank,
                    search_extra=json.loads(arm),
                    token_budget=args.token_budget,
                )
            )
        if args.dump:
            dump_pairs(args.dump, f"B{i}", pairs_b)
        cmp = compare_arms(pairs, pairs_b)
        print(f"\n=== paired comparison (arm B = {arm}, minus arm A) ===")
        print(
            f"conceptual recall@5: A {cmp['recall5_a']:.3f}  B {cmp['recall5_b']:.3f}"
            f"  (n={cmp['n']})"
        )
        sb = summarize(pairs_b)
        if sb.by_type.get("hub", {}).get("n"):
            print(
                f"hub recall@5:        A {s.hub_recall5:.3f}  B {sb.hub_recall5:.3f}"
                f"  tokens/answer mean A {s.mean_tokens:.0f} B {sb.mean_tokens:.0f}"
            )
        print(
            f"arm B: via(top5) {sb.via_top5}  code.search p50 {sb.p50_latency_ms:.0f} ms  "
            f"abstain P/R {sb.abstain_precision:.2f}/{sb.abstain_recall:.2f}  "
            f"wrong abstains {sb.by_type['abstain_probe']['wrong_abstains_on_answerable']}"
            f" (A {s.by_type['abstain_probe']['wrong_abstains_on_answerable']})"
        )
        if sb.by_facet:
            print("arm B by facet:")
            _print_facets(sb, indent="  ")
        if args.track_misses:
            tracked_b = asyncio.run(
                track_misses(
                    pairs_b,
                    args.base_url,
                    args.token,
                    search_extra=json.loads(arm),
                    token_budget=args.token_budget,
                )
            )
            print(f"  track_path on {len(tracked_b)} arm-B conceptual misses:")
            for qid, target, t in tracked_b:
                print(f"    {qid} {target.split('/')[-1][:32]:32} {t.get('verdict', '?')}")
        print("  " + cmp["boot"].line(unit="pp", scale=100.0))
        for name, m in cmp["secondary"].items():
            print(f"conceptual {name}: A {m['a']:.3f}  B {m['b']:.3f}")
            print("  " + m["boot"].line(unit="pp", scale=100.0))
        pa, pb = cmp["lexical_leg_ms_p50"]
        if pa is not None or pb is not None:
            print(f"lexical_leg_ms p50: A {pa}  B {pb}")
        wb = summarize(pairs_b).work_share
        if wb:
            print(
                "  arm B retrieval_work share>0: "
                + "  ".join(f"{k}={v:.3f}" for k, v in wb.items())
            )
        ups = sum(1 for _, d in cmp["moved"] if d > 0)
        print(
            f"  moved on recall@5: {len(cmp['moved'])} questions "
            f"({ups} up, {len(cmp['moved']) - ups} down)"
        )
        for qid, d in cmp["moved"][:12]:
            print(f"    {qid} {d:+.2f}")
        if args.min_compare_lb is not None and cmp["errored_b"]:
            # Pairs with an arm-B error are dropped, so the interval covers a partial n.
            problems.append(
                f"arm {arm}: {cmp['errored_b']} question(s) errored — paired gate refused"
            )
            rc = 1
        if args.min_compare_lb is not None and cmp["boot"].lb < args.min_compare_lb:
            problems.append(
                f"paired recall@5 lower bound {cmp['boot'].lb * 100:+.1f}pp < required "
                f"{args.min_compare_lb * 100:+.1f}pp for arm {arm}"
            )
            rc = 1
    if args.judge:
        rates = []
        for run_i in range(max(1, args.judge_runs)):
            j = asyncio.run(
                judge_answers(
                    questions,
                    args.base_url,
                    args.token,
                    token_budget=args.token_budget,
                )
            )
            rates.append(j["accept_rate"])
            if args.judge_dump:
                dump_verdicts(Path(args.judge_dump), j["verdicts"], run=run_i + 1)
            print(
                f"judged answers run {run_i + 1} (code.ask, {_JUDGE_MODEL}): "
                f"{j['accepted']}/{j['n']} accepted = {j['accept_rate']}"
                f"  abstained {j['abstained']}"
                f"  context tokens mean {j['mean_context_tokens']}"
                f"  answer chars mean {j['mean_answer_chars']}"
                + (f"  errors {j['errors']}" if j["errors"] else "")
            )
            if args.verbose:
                for v in j["verdicts"]:
                    if v["verdict"] != "accept":
                        print(f"  {v['qid']} {v['verdict']:16} {v['query'][:52]}")
                        if v.get("reason"):
                            print(f"      {v['reason'][:160]}")
        mean_rate = round(sum(rates) / len(rates), 3)
        if len(rates) > 1:
            print(
                f"judge accept mean over {len(rates)} runs: {mean_rate} "
                f"(spread {min(rates)}-{max(rates)})"
            )
        if args.min_judge_accept is not None and mean_rate < args.min_judge_accept:
            problems.append(f"judge_accept mean {mean_rate} < {args.min_judge_accept}")
            rc = 1
    for p in problems:
        print(f"GATE FAIL: {p}", file=sys.stderr)
    if rc == 0:
        print("all gates passed")
    return rc


if __name__ == "__main__":
    sys.exit(main())
