"""Routing replay for memory.ask synthesis: freeze retrievals once, replay the
synthesis tier decision offline, price and grade each arm.

The expensive, non-deterministic half of memory.ask is retrieval. The half a router
changes is which model synthesizes. The trick is to freeze the first and replay the
second, so a policy change costs LLM calls only, and every arm sees byte-identical
grounding. This harness does that for loom, driving the REAL handler with
`handle_memory_search` swapped for the frozen output — the same seam the unit tests use
— so recency rerank, hedging, the routing policy (memory/domain/synth_policy.py), the prompts
and the escalation verifier are all the production code paths.

Four sub-commands, run in order:

  sample   Pick N memories from a namespace over HTTP and have an LLM write
           one question each memory answers; the memory is the reference.
             python bench/route_replay.py sample --base-url $LOOM_BASE_URL \\
               --token $TOK --org <org> --namespace <namespace> \\
               -n 60 --out bench/route_questions.jsonl
  capture  Run each question through /v1/memory.search once and freeze the
           full MemorySearchOutput (hits + risk signals).
             python bench/route_replay.py capture --questions q.jsonl \\
               --base-url $LOOM_BASE_URL --token $TOK --out frozen.jsonl
  policy   No LLM calls. Route every frozen item under the current policy and
           print the tier / reason distribution; --sweep varies thresholds.
  replay   Synthesize each frozen item under three arms — strong only, cheap
           only, cascade — price each from the cost accumulator, grade each
           answer against the reference with a fixed judge, and report cost
           saved vs accuracy lost, per routing reason.
             python bench/route_replay.py replay --frozen frozen.jsonl \\
               --strong claude-opus-4-8 --cheap gpt-4o-mini \\
               --judge gpt-4o-2024-08-06 --out bench/route_replay.json
           With --mid <model> two more arms exist: `mid`
           (every ask on the mid model) and `cascade_mid` (the cascade, with
           the strong routes named by --mid-routes answered by the mid
           model). The summary carries `<arm>_vs_strong` for every arm and a
           `routed_bucket` block: cost and accuracy on exactly the asks the
           mid routes moved, against the strong arm on the same asks.

  rejudge  Re-grade an existing results file with the current judge prompt
           (or another judge) without re-synthesizing.

`sample`, `replay` and `rejudge` construct LLM clients in-process, so they need the
provider keys in the environment (OPENAI_API_KEY / ANTHROPIC_API_KEY) and
LOOM_LLM_PROVIDER; the fake provider works for a dry run and prices $0.

Comparability: the judge is held fixed and reference-based (CORRECT / WRONG
against the source memory, verdict first then rationale). It measures
whether the answer preserves the fact the memory holds — the thing the
synthesizer is for — not stylistic preference between models. Identifier
injection is disabled during replay: its extra retrieval is not in the
frozen capture, and enabling it would make arms differ on grounding.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Runnable from anywhere: the server package lives at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench._gate import (
    add_gate_arguments,
    agreement,
    flip_rate,
    gate_from_args,
    majority_label,
    measurement_fields,
    normalize_label,
)

# ---- data shapes -----------------------------------------------------------


@dataclass
class Question:
    qid: str
    query: str
    org: str
    namespace: str
    reference: str = ""
    gold_key: str = ""
    as_of: str | None = None


@dataclass
class Frozen:
    q: Question
    search: dict[str, Any]  # MemorySearchOutput.model_dump()


@dataclass
class ArmResult:
    arm: str
    qid: str
    answer: str = ""
    source: str = ""
    route_tier: str = ""
    route_reason: str = ""
    synth_model: str = ""
    escalated: bool = False
    consistency: str = ""
    consistency_score: float = 0.0
    gate_mode: str = ""
    gate_kept: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    verdict: str = ""  # correct | wrong | abstained | error | ""
    detail: str = ""
    # Citation contract ("cite score"): the keys the
    # answer TEXT actually marks (not the all-recalled fallback the output
    # carries), and +1 for the gold key cited, -1 per other key cited, over
    # the one gold this harness has per question. None when not synthesized
    # or no gold. cite_precision = gold cited / keys cited.
    cited_keys: list[str] = field(default_factory=list)
    cite_score: float | None = None
    # Tokens the synthesizer read (memory.ask context_tokens_est); 0 = unknown.
    context_tokens: int = 0
    cite_precision: float | None = None
    gold_cited: bool | None = None
    # --repeats N: every run's verdict for this qid, the share
    # agreeing with the majority written to `verdict`; cost / llm_calls are
    # per-ask means over the runs, latency the median.
    repeat_verdicts: str = ""
    repeat_agreement: float = 1.0


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def _q_from(row: dict[str, Any]) -> Question:
    return Question(
        qid=str(row["qid"]),
        query=row["query"],
        org=row["org"],
        namespace=row["namespace"],
        reference=row.get("reference", "") or "",
        gold_key=row.get("gold_key", "") or "",
        as_of=row.get("as_of"),
    )


# ---- sample ---------------------------------------------------------------

_QGEN_SYSTEM = (
    "You write ONE realistic question a teammate would ask an assistant whose "
    "memory holds the note below, such that the note is the complete answer. "
    "Name the concrete entity the note is about (PR number, person, setting, "
    "test) so the question is answerable only from this note. Vary the form: "
    "sometimes a direct fact question, sometimes 'what do we know about X', "
    "sometimes a status question. Return ONLY the question."
)


async def cmd_sample(a: argparse.Namespace) -> int:
    import httpx

    from server.llm.client import get_llm_client_for_model

    ident = {
        "org": a.org,
        "namespace": a.namespace,
        "agent": "*",
        "user_id": "*",
        "session_id": "*",
    }
    async with httpx.AsyncClient(
        base_url=a.base_url,
        timeout=60,
        headers={"Authorization": f"Bearer {a.token}"},
    ) as c:
        r = await c.post("/v1/memory.list", json={**ident, "limit": a.pool})
        r.raise_for_status()
        # memory.list entries carry the serialized `value`; a dict/list value is
        # the structured form of the fact, so serialize it as the reference.
        items = []
        for m in r.json().get("entries", []):
            v = m.get("value")
            text = v if isinstance(v, str) else (json.dumps(v) if v not in (None, "") else "")
            if text.strip():
                items.append({**m, "content": text})
    if not items:
        print("no memories with content in that namespace", file=sys.stderr)
        return 2
    rng = random.Random(a.seed)
    picked = rng.sample(items, min(a.n, len(items)))
    llm = get_llm_client_for_model(a.model)
    sem = asyncio.Semaphore(4)
    rows: list[dict[str, Any]] = []

    async def one(i: int, m: dict[str, Any]) -> None:
        content = (m.get("content") or "")[:1500]
        async with sem:
            res = await llm.complete(_QGEN_SYSTEM, f"Note:\n{content}", max_tokens=80)
        q = str(res.text).strip().splitlines()[0].strip().strip('"')
        rows.append(
            {
                "qid": f"s{i:03d}",
                "query": q,
                "org": a.org,
                "namespace": a.namespace,
                "reference": content,
                "gold_key": m.get("memory_key", ""),
            }
        )

    await asyncio.gather(*(one(i, m) for i, m in enumerate(picked)))
    rows.sort(key=lambda r: r["qid"])
    _write_jsonl(a.out, rows)
    print(f"wrote {len(rows)} questions -> {a.out}")
    return 0


# ---- capture --------------------------------------------------------------


async def cmd_capture(a: argparse.Namespace) -> int:
    import httpx

    qs = [_q_from(r) for r in _read_jsonl(a.questions)]
    if a.limit:
        qs = qs[: a.limit]
    # Same over-fetch the ask pipeline does (2x top_k, at least the synthesis
    # window, capped at 40) so the frozen pool is what synthesis would see.
    pool_k = min(max(a.top_k * 2, a.window), 40)
    rows: list[dict[str, Any]] = []
    failed: list[tuple[str, str]] = []
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(
        base_url=a.base_url,
        timeout=90,
        headers={"Authorization": f"Bearer {a.token}"},
    ) as c:

        async def one(q: Question) -> None:
            body: dict[str, Any] = {
                "org": q.org,
                "namespace": q.namespace,
                "agent": "*",
                "query": q.query,
                "top_k": pool_k,
                "auto_touch": False,
            }
            if q.as_of:
                body["valid_as_of"] = q.as_of
            if getattr(a, "search_mode", ""):
                # Pin the retrieval mode of the frozen set: an unpinned capture runs the
                # deployment default.
                body["search_mode"] = a.search_mode
            last = ""
            for attempt in range(3):  # 5xx / transport hiccups: retry, then record
                try:
                    async with sem:
                        r = await c.post("/v1/memory.search", json=body)
                    if r.status_code >= 500:
                        last = f"{r.status_code} {r.text[:200]}"
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    rows.append({"q": asdict(q), "search": r.json()})
                    return
                except httpx.TransportError as e:
                    last = f"{type(e).__name__}: {e}"
                    await asyncio.sleep(1.5 * (attempt + 1))
            failed.append((q.qid, last))
            rows.append(
                {
                    "q": asdict(q),
                    "search": {
                        "results": [],
                        "embedding_model": "",
                        "abstain": True,
                        "abstain_reason": f"capture_error: {last}",
                        "abstain_code": "capture_error",
                    },
                }
            )

        await asyncio.gather(*(one(q) for q in qs))
    rows.sort(key=lambda r: r["q"]["qid"])
    _write_jsonl(a.out, rows)
    n_abstain = sum(1 for r in rows if r["search"].get("abstain"))
    print(f"froze {len(rows)} retrievals ({n_abstain} abstained) -> {a.out}")
    for qid, err in failed:
        print(f"  capture failed (kept as abstain): {qid}: {err}", file=sys.stderr)
    return 0


def _load_frozen(path: str, limit: int = 0) -> list[Frozen]:
    rows = _read_jsonl(path)
    if limit:
        rows = rows[:limit]
    return [Frozen(q=_q_from(r["q"]), search=r["search"]) for r in rows]


# ---- policy (pure) --------------------------------------------------------


def features_of(fz: Frozen, window: int = 15) -> Any:
    """Build the router's features from a frozen retrieval the way ask() does.
    `hedged` approximates the pipeline's post-injection check with the
    retrieval's own entity_absent code (injection is off in replay)."""
    from server.memory.domain.synth_policy import SynthFeatures

    s = fz.search
    res = s.get("results") or []
    n = min(len(res), window)
    top = float(res[0].get("similarity") or 0.0) if res else 0.0
    second = float(res[1].get("similarity") or 0.0) if len(res) > 1 else 0.0
    return SynthFeatures(
        query_chars=len(fz.q.query),
        n_memories=n,
        top_similarity=top,
        similarity_margin=max(0.0, top - second),
        retrieval_confidence=float(s.get("retrieval_confidence") or 0.0),
        retrieval_risk=str(s.get("retrieval_risk") or ""),
        route_agreement=float(s.get("route_agreement") or 0.0),
        hedged=(s.get("abstain_code") == "entity_absent" and n > 0),
        as_of=fz.q.as_of is not None,
        budget_ratio=0.0,
        query=fz.q.query,
    )


def route_distribution(
    frozen: list[Frozen], **thresholds: Any
) -> tuple[Counter[str], Counter[str]]:
    from server.memory.domain.synth_policy import decide

    tiers: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for fz in frozen:
        if fz.search.get("abstain") and fz.search.get("abstain_code") != "entity_absent":
            tiers["(abstained)"] += 1
            continue
        d = decide(features_of(fz), **thresholds)
        tiers[d.tier] += 1
        reasons[d.reason] += 1
    return tiers, reasons


def cmd_policy(a: argparse.Namespace) -> int:
    from server.memory.domain.synth_policy import parse_route_prior

    frozen = _load_frozen(a.frozen, a.limit)
    tiers, reasons = route_distribution(frozen)
    n = len(frozen)
    print(f"# policy over {n} frozen asks (defaults)\n")
    for t, c in tiers.most_common():
        print(f"  {t:<12} {c:>5}  {c / n:6.1%}")
    print("\n  by reason:")
    for r, c in reasons.most_common():
        print(f"    {r:<28} {c:>5}")
    prior = parse_route_prior(getattr(a, "route_prior", "") or "")
    if prior:
        # What the verdict prior would move at each threshold,
        # offline — the cheap share it buys before any LLM is paid for.
        print("\n# verdict prior: cheap share by max contradicted rate")
        for mx in (0.05, 0.10, 0.15, 0.20, 0.25, 0.35, 0.40):
            t, rs = route_distribution(frozen, route_prior=prior, max_cheap_prior=mx)
            routed = t["cheap"] + t["strong"]
            share = t["cheap"] / routed if routed else 0.0
            moved = sum(c for r, c in rs.items() if r.endswith(":prior_cheap"))
            print(f"  max {mx:.2f}  cheap {share:6.1%}  moved by prior {moved:>5}")
    if a.sweep:
        print("\n# sweep: cheap share under other thresholds")
        print(f"  {'min_conf':>8} {'min_top':>7} {'max_qchars':>10} {'cheap%':>7}")
        for mc in (0.5, 0.55, 0.6, 0.65, 0.7):
            for mt in (0.55, 0.65, 0.7, 0.75):
                for qc in (120, 160, 240):
                    t, _ = route_distribution(
                        frozen,
                        min_cheap_confidence=mc,
                        min_cheap_top_similarity=mt,
                        max_cheap_query_chars=qc,
                    )
                    routed = t["cheap"] + t["strong"]
                    share = t["cheap"] / routed if routed else 0.0
                    print(f"  {mc:>8.2f} {mt:>7.2f} {qc:>10} {share:>7.1%}")
    return 0


# ---- replay ---------------------------------------------------------------

_CURRENT: contextvars.ContextVar[Frozen | None] = contextvars.ContextVar(
    "route_replay_frozen", default=None
)

_JUDGE_SYSTEM = (
    "You grade whether an assistant's answer preserves the fact held in ONE "
    "reference note. The assistant saw MANY notes, so its answer may contain "
    "additional facts, dates, related items or context that the reference does "
    "not mention — that is expected and is NEVER grounds for WRONG. Judge only "
    "the reference's own fact. Output CORRECT if the answer states that fact "
    "accurately about the asked entity (paraphrase, hedged phrasing, or extra "
    "detail around it are all fine). Output WRONG only if the answer "
    "contradicts the reference's fact, attributes it to a different entity, "
    "fails to state it at all, or buries it so a reader could not tell it is "
    "the answer. First line: exactly CORRECT or WRONG. Then one sentence."
)


def _install_replay_patches() -> None:
    """Swap retrieval and budget for the frozen item bound to the task, and
    turn off the pipeline steps whose inputs are not in the capture."""
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.memory.api.models_search import MemorySearchOutput
    from server.settings import settings

    async def frozen_search(search_input: Any) -> MemorySearchOutput:
        fz = _CURRENT.get()
        assert fz is not None, "replay task without a frozen item bound"
        out = MemorySearchOutput.model_validate(fz.search)
        k = int(getattr(search_input, "top_k", 0) or 0)
        if k and len(out.results) > k:
            out.results = out.results[:k]
        return out

    async def ratio(org: str, ns: str) -> float:
        return 0.0

    async def over_cap(org: str, ns: str) -> bool:
        return False

    hsearch.handle_memory_search = frozen_search  # type: ignore[assignment]
    budget.namespace_budget_ratio = ratio  # type: ignore[assignment]
    budget.namespace_over_cap = over_cap  # type: ignore[assignment]
    settings.memory_ask_identifier_injection = False
    # A replay measures synthesis; a served precedent would measure the store.
    settings.memory_ask_precedents = False
    # Same for the exact-prompt response cache: an arm that
    # re-asks a frozen question must reach the provider.
    settings.memory_ask_llm_cache_default = False
    # Code-plane grounding reads the code index live; its additions are not in
    # the capture, so every arm would otherwise differ on grounding by luck.
    settings.memory_ask_pr_temporal_gate = False
    settings.memory_ask_code_enrichment = False
    settings.memory_ask_pr_grounding = False
    settings.memory_ask_code_staleness = False


MID_ARMS = ("mid", "cascade_mid")


def _arm_settings(
    arm: str, strong: str, cheap: str, mid: str = "", mid_routes: str = "uncertain"
) -> None:
    from server.settings import settings

    settings.memory_ask_mid_model = ""
    settings.memory_ask_synth_mid_routes = mid_routes or "uncertain"
    if arm == "strong":
        settings.memory_ask_synth_model, settings.memory_ask_synth_model_cheap = strong, ""
    elif arm == "cheap":
        settings.memory_ask_synth_model, settings.memory_ask_synth_model_cheap = cheap, ""
    elif arm == "cascade":
        settings.memory_ask_synth_model, settings.memory_ask_synth_model_cheap = strong, cheap
    elif arm in MID_ARMS:
        if not mid:
            raise SystemExit(f"arm {arm} needs --mid <model>")
        if arm == "mid":
            settings.memory_ask_synth_model, settings.memory_ask_synth_model_cheap = mid, ""
        else:
            settings.memory_ask_synth_model, settings.memory_ask_synth_model_cheap = strong, cheap
            settings.memory_ask_mid_model = mid
    else:
        raise SystemExit(f"unknown arm {arm}")


def ask_input_for(fz: Frozen, top_k: int, *, credit: bool = False) -> Any:
    """The memory.ask request one replayed item makes. The replay REPORTS the
    consistency label (false_memory_rate, `contradicted`), so it pins the judge
    inline whatever the deployment default is (under the `deferred` default the
    wire label is empty by design)."""
    from server.memory.api.models_ask import MemoryAskInput

    return MemoryAskInput(
        org=fz.q.org,
        namespace=fz.q.namespace,
        agent="*",
        query=fz.q.query,
        top_k=top_k,
        as_of=fz.q.as_of,
        consistency="inline",
        # A replay is an evaluation read — it credits nothing
        # unless --credit.
        **measurement_fields("ask", credit=credit),
    )


async def _run_arm(arm: str, frozen: list[Frozen], a: argparse.Namespace) -> list[ArmResult]:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.observability import cost_accumulator as ca

    _arm_settings(
        arm,
        a.strong,
        a.cheap,
        getattr(a, "mid", "") or "",
        getattr(a, "mid_routes", "") or "uncertain",
    )
    sem = asyncio.Semaphore(a.concurrency)
    out: list[ArmResult] = []

    async def one(fz: Frozen) -> None:
        res = ArmResult(arm=arm, qid=fz.q.qid)
        _CURRENT.set(fz)
        acc = ca.begin_op()
        t0 = time.perf_counter()
        try:
            async with sem:
                o = await handle_memory_ask(
                    ask_input_for(fz, a.top_k, credit=bool(getattr(a, "credit", False)))
                )
            res.answer, res.source = o.answer, o.source
            res.route_tier, res.route_reason = o.route_tier, o.route_reason
            res.synth_model, res.escalated = o.synth_model, o.escalated
            res.consistency = str(getattr(o, "consistency", "") or "")
            res.consistency_score = float(getattr(o, "consistency_score", 0.0) or 0.0)
            res.gate_mode = str(getattr(o, "gate_mode", "") or "")
            res.gate_kept = int(getattr(o, "gate_kept", 0) or 0)
            res.context_tokens = int(getattr(o, "context_tokens_est", 0) or 0)
            if o.abstain or not o.answer.strip():
                res.verdict = "abstained"
            res.cited_keys, res.cite_score, res.cite_precision, res.gold_cited = cite_metrics(
                o.answer,
                o.results,
                fz.q.gold_key,
                o.source,
            )
        except Exception as e:  # noqa: BLE001 — record, keep replaying
            res.verdict, res.detail = "error", f"{type(e).__name__}: {e}"
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        # llm_calls arrives with the agent_events.model change; 0 until then.
        res.cost_usd, res.llm_calls = acc.cost_usd, int(getattr(acc, "llm_calls", 0))
        out.append(res)

    # Each item in its own task so the contextvar binding is per-item.
    await asyncio.gather(*(asyncio.create_task(one(fz)) for fz in frozen))
    out.sort(key=lambda r: r.qid)
    return out


async def _judge(
    results: list[ArmResult], by_qid: dict[str, Frozen], a: argparse.Namespace
) -> float:
    """Reference-based grading with a fixed judge. Returns judge spend."""
    from server.llm.client import get_llm_client_for_model
    from server.observability import cost_accumulator as ca

    judge = get_llm_client_for_model(a.judge)
    sem = asyncio.Semaphore(a.concurrency)
    acc = ca.begin_op()

    async def one(r: ArmResult) -> None:
        if r.verdict:  # abstained / error already decided
            return
        fz = by_qid[r.qid]
        if not fz.q.reference:
            r.verdict, r.detail = "", "no reference"
            return
        user = f"Question: {fz.q.query}\n\nReference note:\n{fz.q.reference}\n\nAnswer:\n{r.answer}"
        try:
            async with sem:
                res = await judge.complete(_JUDGE_SYSTEM, user, max_tokens=120)
            first = str(res.text).strip().splitlines()[0].strip().upper()
            r.verdict = "correct" if first.startswith("CORRECT") else "wrong"
            r.detail = str(res.text).strip()[:300]
        except Exception as e:  # noqa: BLE001
            r.verdict, r.detail = "error", f"judge {type(e).__name__}: {e}"

    await asyncio.gather(*(asyncio.create_task(one(r)) for r in results))
    return acc.cost_usd


def _labels_of(rows: list[ArmResult]) -> dict[str, list[str]]:
    """{qid: per-run CORRECT/WRONG labels} for the gate; abstained counts
    WRONG, errored runs ERROR (outvoted by judged runs)."""
    out: dict[str, list[str]] = {}
    for r in rows:
        reps = [v for v in (r.repeat_verdicts or "").split("|") if v]
        out[r.qid] = [normalize_label(v) for v in (reps or [r.verdict])]
    return out


def fold_arm_repeats(runs: list[list[ArmResult]]) -> list[ArmResult]:
    """One row per qid from N replays of an arm: the first
    run's row with `verdict` replaced by the MAJORITY (correct when more than
    half the graded runs were; abstained only when every run abstained; wrong
    otherwise), cost / llm_calls averaged per ask, latency the median."""
    if len(runs) == 1:
        return runs[0]
    by_qid: dict[str, list[ArmResult]] = defaultdict(list)
    for rows in runs:
        for r in rows:
            by_qid[r.qid].append(r)
    out: list[ArmResult] = []
    for qid in sorted(by_qid):
        rs = by_qid[qid]
        base = rs[0]
        verdicts = [r.verdict for r in rs]
        labels = [normalize_label(v) for v in verdicts]
        maj = majority_label(labels)
        if maj == "CORRECT":
            base.verdict = "correct"
        elif all(v == "abstained" for v in verdicts):
            base.verdict = "abstained"
        elif maj == "WRONG":
            base.verdict = "wrong"
        base.repeat_verdicts = "|".join(verdicts)
        base.repeat_agreement = agreement(labels)
        base.cost_usd = round(sum(r.cost_usd for r in rs) / len(rs), 6)
        base.llm_calls = int(round(sum(r.llm_calls for r in rs) / len(rs)))
        base.latency_ms = int(statistics.median(r.latency_ms for r in rs))
        out.append(base)
    return out


def cite_metrics(
    answer: str,
    results: list[Any],
    gold_key: str,
    source: str,
) -> tuple[list[str], float | None, float | None, bool | None]:
    """(cited_keys, cite_score, cite_precision, gold_cited) for one answer.

    Uses the answer TEXT's markers, via the production `_keys_cited`, so the output's
    "cited or all recalled" fallback cannot inflate the score. The cite score is +1 per
    gold key cited, -1 per non-gold key cited, over the number of gold keys (one here).
    Not synthesized or no gold → all None. Cited nothing → score 0, precision None,
    gold_cited False."""
    if source != "synthesized" or not gold_key:
        return [], None, None, None
    from server.memory.app.ask.result import _keys_cited

    cited = list(_keys_cited(answer or "", results or []))
    if not cited:
        return [], 0.0, None, False
    gold_cited = gold_key in cited
    correct = 1 if gold_cited else 0
    score = float(correct - (len(cited) - correct))
    return cited, score, round(correct / len(cited), 4), gold_cited


ACCURACY_DENOMINATOR = "graded = correct + wrong; abstained and errored asks are left out"
GATE_DENOMINATOR = "correct + wrong + abstained; an abstention counts wrong, as in the gate"


def _accuracy_fields(rows: list[ArmResult], graded: list[ArmResult]) -> dict[str, Any]:
    """Accuracy over two stated denominators. `accuracy` reads only the
    judged answers; `accuracy_abstain_wrong` also counts every abstention as
    wrong, which is the verdict `--gate` reads (errored asks are out of both)."""
    correct = sum(1 for r in graded if r.verdict == "correct")
    abstained = sum(1 for r in rows if r.verdict == "abstained")
    with_abstain = len(graded) + abstained
    return {
        "graded": len(graded),
        "correct": correct,
        "accuracy": round(correct / len(graded), 3) if graded else None,
        "accuracy_denominator": ACCURACY_DENOMINATOR,
        "abstained": abstained,
        "accuracy_abstain_wrong": round(correct / with_abstain, 3) if with_abstain else None,
        "accuracy_abstain_wrong_denominator": GATE_DENOMINATOR,
        "errors": sum(1 for r in rows if r.verdict == "error"),
    }


def summarize(results: dict[str, list[ArmResult]]) -> dict[str, Any]:
    """Pure aggregation, so the numbers in the report are testable."""
    summary: dict[str, Any] = {"arms": {}}
    for arm, rows in results.items():
        graded = [r for r in rows if r.verdict in ("correct", "wrong")]
        scored = [r for r in rows if r.cite_score is not None]
        precisions = [r.cite_precision for r in scored if r.cite_precision is not None]
        answered = [
            r
            for r in rows
            if r.source in ("synthesized", "precedent") and r.verdict not in ("abstained", "error")
        ]
        toks = [r.context_tokens for r in rows if r.context_tokens > 0]
        summary["arms"][arm] = {
            # Citation contract per arm (None when no synthesized answer had a gold).
            "cite_score_mean": round(
                statistics.fmean(float(r.cite_score or 0.0) for r in scored), 3
            )
            if scored
            else None,
            "cite_precision_mean": round(statistics.fmean(precisions), 3) if precisions else None,
            "gold_cited_share": round(sum(1 for r in scored if r.gold_cited) / len(scored), 3)
            if scored
            else None,
            # False memory rate: of the answers the arm actually gave, the share whose
            # consistency verdict says the answer contradicts its own cited memories
            # (asserts content the store does not hold). Reported beside accuracy — a
            # hedge or a collapse that lowers this without moving accuracy is a real
            # gain.
            "answered": len(answered),
            "consistency_checked": sum(1 for r in answered if r.consistency),
            "false_memory_rate": round(
                sum(1 for r in answered if r.consistency == "contradicted") / len(answered), 3
            )
            if answered
            else None,
            # Tokens per question the synthesizer read; a change that lifts
            # accuracy by adding tokens is a different decision from one that
            # does not.
            "context_tokens_mean": int(statistics.fmean(toks)) if toks else None,
            "context_tokens_p90": (
                int(sorted(toks)[min(len(toks) - 1, -(-9 * len(toks) // 10) - 1)]) if toks else None
            ),
            "n": len(rows),
            "cost_usd": round(sum(r.cost_usd for r in rows), 4),
            "cost_per_ask": round(sum(r.cost_usd for r in rows) / len(rows), 5) if rows else 0.0,
            "latency_p50_ms": int(statistics.median(r.latency_ms for r in rows)) if rows else 0,
            **_accuracy_fields(rows, graded),
            # Per-item flip rate over --repeats verdicts.
            "flip_rate": flip_rate(
                [normalize_label(v) for v in (r.repeat_verdicts or "").split("|") if v]
                for r in rows
            ),
        }
    strong = summary["arms"].get("strong")
    # Paired per-question bootstrap (bench/bootstrap.py) of every other arm
    # against strong-only: the interval is what n=60 actually supports, and
    # the reason a one-question delta is reported as noise rather than as a
    # win or a loss. `<arm>_vs_strong` per arm.
    for arm, casc in summary["arms"].items():
        if arm == "strong" or not strong or strong["cost_usd"] <= 0:
            continue
        summary[f"{arm}_vs_strong"] = _paired_vs_strong(
            results["strong"], results[arm], strong, casc
        )
    for arm in results:
        if not arm.startswith("cascade"):
            continue
        rows = results[arm]
        by_reason: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"n": 0, "correct": 0, "graded": 0}
        )
        for r in rows:
            base = r.route_reason.split(":")[0] or "(none)"
            b = by_reason[base]
            b["n"] += 1
            if r.verdict in ("correct", "wrong"):
                b["graded"] += 1
                b["correct"] += r.verdict == "correct"
        summary[arm] = {
            "tiers": dict(Counter(r.route_tier for r in rows)),
            "escalated": sum(1 for r in rows if r.escalated),
            "consistency": dict(Counter(r.consistency for r in rows if r.consistency)),
            "contradicted": sum(1 for r in rows if r.consistency == "contradicted"),
            "cheap_attempts": sum(1 for r in rows if r.route_tier == "cheap" or r.escalated),
            "by_reason": {
                k: {**v, "accuracy": round(v["correct"] / v["graded"], 3) if v["graded"] else None}
                for k, v in sorted(by_reason.items())
            },
        }
        if "strong" in results:
            summary[arm]["routed_bucket"] = routed_bucket(results["strong"], rows)
    # Where the cheap arm alone was wrong but strong right: the questions a
    # router MUST send strong. Grouped by the cascade's reason for them.
    if {"cheap", "strong", "cascade"} <= results.keys():
        s = {r.qid: r for r in results["strong"]}
        c = {r.qid: r for r in results["cheap"]}
        k = {r.qid: r for r in results["cascade"]}
        must_strong = [
            qid
            for qid in s
            if s[qid].verdict == "correct" and c.get(qid) and c[qid].verdict == "wrong"
        ]
        caught = sum(1 for qid in must_strong if k[qid].route_tier == "strong")
        summary["router_recall_on_hard"] = {
            "hard_questions": len(must_strong),
            "routed_or_escalated_strong": caught,
            "recall": round(caught / len(must_strong), 3) if must_strong else None,
            "missed_qids": [qid for qid in must_strong if k[qid].route_tier != "strong"],
        }
    return summary


def _paired_vs_strong(
    strong_rows: list[ArmResult],
    arm_rows: list[ArmResult],
    strong: dict[str, Any],
    arm: dict[str, Any],
) -> dict[str, Any]:
    try:
        from bootstrap import paired_bootstrap
    except ImportError:
        from bench.bootstrap import paired_bootstrap

    out: dict[str, Any] = {
        "cost_saved_pct": round(100 * (1 - arm["cost_usd"] / strong["cost_usd"]), 1),
        "accuracy_delta_pp": (
            round(100 * (arm["accuracy"] - strong["accuracy"]), 1)
            if arm["accuracy"] is not None and strong["accuracy"] is not None
            else None
        ),
    }
    s_by = {r.qid: r for r in strong_rows}
    acc_deltas: list[float] = []
    cost_deltas: list[float] = []
    for r in arm_rows:
        sr = s_by.get(r.qid)
        if sr is None:
            continue
        cost_deltas.append(sr.cost_usd - r.cost_usd)
        if r.verdict in ("correct", "wrong") and sr.verdict in ("correct", "wrong"):
            acc_deltas.append(float(r.verdict == "correct") - float(sr.verdict == "correct"))
    if acc_deltas:
        b = paired_bootstrap(acc_deltas)
        out["accuracy_delta_95_pp"] = [round(100 * b.lb, 1), round(100 * b.ub, 1)]
        out["accuracy_wtl"] = [b.wins, b.ties, b.losses]
        out["mcnemar_p"] = round(mcnemar_p(b.wins, b.losses), 4)
    if cost_deltas:
        bc = paired_bootstrap(cost_deltas)
        out["cost_saved_usd_95"] = [
            round(bc.lb * len(cost_deltas), 4),
            round(bc.ub * len(cost_deltas), 4),
        ]
    return out


def mcnemar_p(wins: int, losses: int) -> float:
    """Exact McNemar (two-sided binomial on the discordant pairs): the
    probability, under 'the arms are equally accurate', of a win/loss split
    at least this lopsided. 1.0 with no discordant pairs."""
    from math import comb

    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def routed_bucket(
    strong_rows: list[ArmResult],
    arm_rows: list[ArmResult],
    suffix: str = ":mid",
) -> dict[str, Any]:
    """The asks the arm's router handed to the mid tier
    (route_reason carries `:mid`, escalated ones too), against the strong arm
    on the same asks — the bucket's own $/ask and accuracy, since the rest
    of the corpus is the same call in both arms."""
    s_by = {r.qid: r for r in strong_rows}
    # `<rule>:mid` or `<rule>:mid:mid_uncited` — the second token names the tier.
    tag = suffix.lstrip(":")
    moved = [r for r in arm_rows if (r.route_reason or "").split(":")[1:2] == [tag]]
    pairs = [(s_by[r.qid], r) for r in moved if r.qid in s_by]
    if not pairs:
        return {"n": 0}
    ok = ("correct", "wrong")
    graded = [(s, r) for s, r in pairs if s.verdict in ok and r.verdict in ok]
    s_correct = sum(1 for s, _ in graded if s.verdict == "correct")
    a_correct = sum(1 for _, r in graded if r.verdict == "correct")
    wins = sum(1 for s, r in graded if r.verdict == "correct" and s.verdict == "wrong")
    losses = sum(1 for s, r in graded if r.verdict == "wrong" and s.verdict == "correct")
    s_cost = sum(s.cost_usd for s, _ in pairs)
    a_cost = sum(r.cost_usd for _, r in pairs)
    return {
        "n": len(pairs),
        "escalated": sum(1 for _, r in pairs if r.escalated),
        "strong_cost_per_ask": round(s_cost / len(pairs), 5),
        "arm_cost_per_ask": round(a_cost / len(pairs), 5),
        "cost_saved_pct": round(100 * (1 - a_cost / s_cost), 1) if s_cost > 0 else None,
        "graded": len(graded),
        "strong_accuracy": round(s_correct / len(graded), 3) if graded else None,
        "arm_accuracy": round(a_correct / len(graded), 3) if graded else None,
        "wtl": [wins, len(graded) - wins - losses, losses],
        "mcnemar_p": round(mcnemar_p(wins, losses), 4),
    }


def _print_summary(s: dict[str, Any]) -> None:
    print("\n# route_replay summary\n")
    print(
        f"  {'arm':<9}{'n':>5}{'cost$':>10}{'$/ask':>10}{'p50ms':>8}"
        f"{'acc':>7}{'acc_aw':>8}{'abst':>6}{'err':>5}{'fmr':>7}{'ctx':>7}"
    )
    for arm, v in s["arms"].items():
        acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "-"
        acc_aw = v.get("accuracy_abstain_wrong")
        acc_aw = f"{acc_aw:.1%}" if acc_aw is not None else "-"
        fmr = f"{v['false_memory_rate']:.1%}" if v.get("false_memory_rate") is not None else "-"
        ctx = v.get("context_tokens_mean")
        print(
            f"  {arm:<9}{v['n']:>5}{v['cost_usd']:>10.4f}{v['cost_per_ask']:>10.5f}"
            f"{v['latency_p50_ms']:>8}{acc:>7}{acc_aw:>8}{v['abstained']:>6}{v['errors']:>5}"
            f"{fmr:>7}{(ctx if ctx is not None else '-'):>7}"
        )
    print(f"\n  acc: {ACCURACY_DENOMINATOR}.")
    print(f"  acc_aw: {GATE_DENOMINATOR}.")
    print("  The paired deltas below read asks both arms had judged.")
    for arm in s["arms"]:
        cv = s.get(f"{arm}_vs_strong")
        if not cv:
            continue
        ci = cv.get("accuracy_delta_95_pp")
        wtl = "/".join(map(str, cv.get("accuracy_wtl", []))) if cv.get("accuracy_wtl") else ""
        p = f"; McNemar p {cv['mcnemar_p']}" if "mcnemar_p" in cv else ""
        band = (
            f" (95% paired bootstrap {ci[0]:+.1f} .. {ci[1]:+.1f} pp; w/t/l {wtl}{p})" if ci else ""
        )
        print(
            f"\n  {arm} vs strong: cost saved {cv['cost_saved_pct']}%, "
            f"accuracy delta {cv['accuracy_delta_pp']} pp{band}"
        )
    for arm in s["arms"]:
        c = s.get(arm) if arm.startswith("cascade") else None
        if not c:
            continue
        print(
            f"  {arm} tiers {c['tiers']}, escalated {c['escalated']} of "
            f"{c['cheap_attempts']} cheap attempts"
        )
        print("  by reason:")
        for k, v in c["by_reason"].items():
            acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "-"
            print(f"    {k:<26}{v['n']:>5}  acc {acc}")
        rb = c.get("routed_bucket") or {}
        if rb.get("n"):
            print(
                f"  routed bucket (mid routes): n {rb['n']}, escalated {rb['escalated']}, "
                f"$/ask strong {rb['strong_cost_per_ask']} -> arm {rb['arm_cost_per_ask']} "
                f"({rb['cost_saved_pct']}% saved); accuracy strong {rb['strong_accuracy']} -> "
                f"arm {rb['arm_accuracy']}, w/t/l {'/'.join(map(str, rb['wtl']))}, "
                f"McNemar p {rb['mcnemar_p']}"
            )
    if "router_recall_on_hard" in s:
        h = s["router_recall_on_hard"]
        print(
            f"\n  hard questions (cheap wrong, strong right): {h['hard_questions']}; "
            f"router sent strong: {h['routed_or_escalated_strong']} "
            f"(recall {h['recall']}); missed: {h['missed_qids']}"
        )


async def cmd_replay(a: argparse.Namespace) -> int:
    frozen = _load_frozen(a.frozen, a.limit)
    if not frozen:
        print("no frozen items", file=sys.stderr)
        return 2
    _install_replay_patches()
    by_qid = {fz.q.qid: fz for fz in frozen}
    arms = [x.strip() for x in a.arms.split(",") if x.strip()]
    results: dict[str, list[ArmResult]] = {}
    judge_cost = 0.0
    repeats = max(1, int(getattr(a, "repeats", 1) or 1))
    for arm in arms:
        runs: list[list[ArmResult]] = []
        for _rep in range(repeats):
            t0 = time.perf_counter()
            rows = await _run_arm(arm, frozen, a)
            print(
                f"  arm {arm}: {len(rows)} asks, "
                f"${sum(r.cost_usd for r in rows):.4f}, {time.perf_counter() - t0:.0f}s",
                file=sys.stderr,
            )
            if a.judge:
                judge_cost += await _judge(rows, by_qid, a)
            runs.append(rows)
        results[arm] = fold_arm_repeats(runs)
    summary = summarize(results)
    if getattr(a, "gate", False):
        # The verdict every arm reads against strong — the
        # paired clustered bootstrap over MAJORITY verdicts, flip rate beside
        # it, pass|fail|unresolved against the measured band.
        for arm in arms:
            if arm == "strong" or "strong" not in results:
                continue
            g = gate_from_args(a, _labels_of(results["strong"]), _labels_of(results[arm]))
            summary[f"{arm}_gate"] = g.as_dict()
            print(f"\n  {arm} vs strong (accuracy gate):")
            for line in g.lines():
                print("    " + line)
    summary["config"] = {
        "strong": a.strong,
        "cheap": a.cheap,
        "judge": a.judge,
        "top_k": a.top_k,
        "mid": getattr(a, "mid", "") or "",
        "mid_routes": getattr(a, "mid_routes", "") or "",
        "frozen": a.frozen,
        "n": len(frozen),
        "judge_cost_usd": round(judge_cost, 4),
    }
    _print_summary(summary)
    if a.out:
        Path(a.out).write_text(
            json.dumps(
                {
                    "summary": summary,
                    "rows": {arm: [asdict(r) for r in rows] for arm, rows in results.items()},
                },
                indent=1,
                default=str,
            )
        )
        print(f"\nwrote {a.out}")
    return 0


async def cmd_rejudge(a: argparse.Namespace) -> int:
    """Re-grade the answers in an existing results file with the current judge
    prompt (or a different judge) — no synthesis, so a judge iteration costs
    judge calls only. Rewrites the file's summary and verdicts in place."""
    data = json.loads(Path(a.results).read_text())
    cfg = data.get("summary", {}).get("config", {})
    frozen = _load_frozen(a.frozen or cfg.get("frozen", ""))
    by_qid = {fz.q.qid: fz for fz in frozen}
    results: dict[str, list[ArmResult]] = {}
    judge_cost = 0.0
    for arm, rows in data["rows"].items():
        rs = [ArmResult(**r) for r in rows]
        for r in rs:
            if r.verdict in ("correct", "wrong"):
                r.verdict, r.detail = "", ""
        judge_cost += await _judge(rs, by_qid, a)
        results[arm] = rs
    summary = summarize(results)
    summary["config"] = {
        **cfg,
        "judge": a.judge,
        "rejudged": True,
        "judge_cost_usd": round(judge_cost, 4),
    }
    _print_summary(summary)
    out = a.out or a.results
    Path(out).write_text(
        json.dumps(
            {
                "summary": summary,
                "rows": {arm: [asdict(r) for r in rows] for arm, rows in results.items()},
            },
            indent=1,
            default=str,
        )
    )
    print(f"\nwrote {out}")
    return 0


# ---- cli ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="LLM-write one question per sampled memory")
    s.add_argument("--base-url", required=True)
    s.add_argument("--token", required=True)
    s.add_argument("--org", required=True)
    s.add_argument("--namespace", required=True)
    s.add_argument("-n", type=int, default=60)
    s.add_argument("--pool", type=int, default=1000, help="memory.list limit to sample from")
    s.add_argument("--model", default="gpt-4o-mini")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", required=True)

    c = sub.add_parser("capture", help="freeze memory.search output per question")
    c.add_argument("--questions", required=True)
    c.add_argument("--base-url", required=True)
    c.add_argument("--token", required=True)
    c.add_argument("--top-k", type=int, default=15)
    c.add_argument(
        "--search-mode",
        default="",
        help="Pin memory.search's search_mode for the frozen set; empty = the "
        "deployment default (memory_search_default_mode).",
    )
    c.add_argument("--window", type=int, default=15, help="LOOM_MEMORY_ASK_CONTEXT_MEMORIES")
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--out", required=True)

    o = sub.add_parser("policy", help="route the frozen set, no LLM calls")
    o.add_argument("--frozen", required=True)
    o.add_argument("--limit", type=int, default=0)
    o.add_argument("--sweep", action="store_true")
    o.add_argument(
        "--route-prior",
        default="",
        help="'measured' or a JSON route->rate map; prints the cheap "
        "share the verdict prior buys at each threshold",
    )

    r = sub.add_parser("replay", help="synthesize + grade each arm on the frozen set")
    r.add_argument("--frozen", required=True)
    r.add_argument("--strong", required=True)
    r.add_argument("--cheap", required=True)
    r.add_argument(
        "--judge", default="gpt-4o-2024-08-06", help="fixed reference-based judge; '' skips grading"
    )
    r.add_argument(
        "--mid",
        default="",
        help="the mid-tier model for the `mid` and `cascade_mid` "
        "arms (e.g. claude-sonnet-5, claude-haiku-4-5-20251001)",
    )
    r.add_argument(
        "--mid-routes",
        default="uncertain",
        help="strong routes the mid tier answers in `cascade_mid`: 'uncertain' "
        "(weak_top_hit, low_confidence, retrieval_risk_high) or a comma list",
    )
    r.add_argument(
        "--arms",
        default="strong,cheap,cascade",
        help="comma list of strong, cheap, cascade, mid, cascade_mid",
    )
    r.add_argument("--top-k", type=int, default=15)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--out", default="")
    add_gate_arguments(r)  # --repeats / --gate* / --credit / --judge-calibration

    j = sub.add_parser("rejudge", help="re-grade an existing results file, no synthesis")
    j.add_argument("--results", required=True)
    j.add_argument("--frozen", default="", help="defaults to the path recorded in the results")
    j.add_argument("--judge", default="gpt-4o-2024-08-06")
    j.add_argument("--concurrency", type=int, default=4)
    j.add_argument("--out", default="", help="defaults to overwriting --results")

    a = p.parse_args(argv)
    fn = {
        "sample": cmd_sample,
        "capture": cmd_capture,
        "replay": cmd_replay,
        "rejudge": cmd_rejudge,
    }.get(a.cmd)
    if a.cmd == "policy":
        return cmd_policy(a)
    assert fn is not None
    return asyncio.run(fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
