"""Acceptance bench — the autonomous QA agent over a large repository.

Precision first: localization on the authored code-QA
fixture (`bench/fixtures/code_intel_qa.jsonl`, scored by
`bench/code_intel_qa.py`'s scoring core — recall@5 on conceptual questions,
exact@1 on symbol definitions, caller-set F1, abstain precision). Economy
second: retrieval calls per answered question (retries included — the QA
agent values fewer tool calls), mean `tokens_returned`, p50 wall.

Every call goes through `loom.reference_client.ReferenceClient`: consumer tag,
timeouts, fail-soft retries, and the outcome feedback the QA agent posts
(`code.outcome used_in_answer` on a localized answer, `ignored` on a miss).

    python bench/acceptance/code_agent.py --base-url http://127.0.0.1:7893 \\
        --token-file bench.token --namespace code-clickhouse
    python bench/acceptance/code_agent.py --dry      # synthetic retrievals, no server
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "sdk" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from loom.reference_client import ReferenceClient  # noqa: E402

from bench import code_intel_qa as cq  # noqa: E402
from bench.acceptance._common import (  # noqa: E402
    Metric,
    Report,
    add_common_args,
    finish,
    make_client,
    p50,
    ratio,
)

FIXTURE = cq._FIXTURE


async def retrieve(client: ReferenceClient, q: cq.Question) -> tuple[cq.Retrieval, str]:
    """One question → the bench's Retrieval plus the answer's trace_run_id
    (for the outcome report). Mirrors `code_intel_qa._ask`, through the
    reference client."""
    t0 = time.perf_counter()
    if q.qtype == "callers":
        r = await client.code_callers(q.gold["symbol"], depth=1)
        if not r.ok:
            return cq.Retrieval(error=f"{r.code}: {r.error}"), ""
        edges = r.data.get("edges") or []
        return cq.Retrieval(
            callers=sorted({str(e.get("from_symbol") or "") for e in edges}),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        ), str(r.data.get("trace_run_id") or "")
    if q.qtype == "symbol_def":
        r = await client.code_symbol(q.query)
        if not r.ok:
            return cq.Retrieval(error=f"{r.code}: {r.error}"), ""
        defs = r.data.get("definitions") or []
        return cq.Retrieval(
            top_symbol=str(defs[0].get("symbol") or "") if defs else "",
            paths=[str(d.get("path") or "") for d in defs],
            tokens_returned=int(r.data.get("tokens_returned") or 0),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        ), str(r.data.get("trace_run_id") or "")
    # conceptual + abstain_probe: code.search, the PR leg pinned before a
    # mined question's fix (inert on the authored set)
    pinned: dict[str, Any] = (
        {"pr_leg_before": str(q.source["merged_at"]).replace(" ", "T")}
        if q.source.get("merged_at")
        else {}
    )
    r = await client.code_search(q.query, top_k=10, **pinned)
    if not r.ok:
        return cq.Retrieval(error=f"{r.code}: {r.error}"), ""
    results = r.data.get("results") or []
    return cq.Retrieval(
        paths=[str(u.get("path") or "") for u in results],
        abstained=bool(r.data.get("abstain")),
        tokens_returned=int(r.data.get("tokens_returned") or 0),
        work={str(k): int(v) for k, v in (r.data.get("retrieval_work") or {}).items()},
        via=[str(u.get("via") or "") for u in results],
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        route=str(r.data.get("route") or ""),
    ), str(r.data.get("trace_run_id") or "")


def outcome_for(q: cq.Question, r: cq.Retrieval) -> tuple[str, list[str]] | None:
    """The QA agent's feedback for one answered conceptual question: the
    gold path it used when localization succeeded, the top path it ignored
    when it did not. None when nothing was served or the type has no path
    answer (pure)."""
    if q.qtype != "conceptual" or r.error or r.abstained or not r.paths:
        return None
    gold = [str(p) for p in q.gold.get("paths") or []]
    for p in r.paths[:5]:
        if cq.recall_at_k(gold, [p], 1) > 0:
            return "used_in_answer", [p]
    return "ignored", [r.paths[0]]


def economy(
    summary: cq.Summary, *, retrieval_attempts: int, latencies: list[float]
) -> dict[str, float | None]:
    answered = summary.n - summary.errors
    return {
        "calls_per_answer": ratio(retrieval_attempts, answered),
        "mean_tokens": summary.mean_tokens,
        "p50_latency_ms": p50(latencies),
    }


def build_report(
    args: argparse.Namespace,
    summary: cq.Summary,
    econ: dict[str, float | None],
    *,
    outcomes_posted: int,
    probe_line: str,
) -> Report:
    rep = Report(
        bench="code_agent",
        archetype="autonomous QA agent over a large repository",
        base_url=args.base_url,
        consumer=args.consumer,
        probe=probe_line,
    )
    by = summary.by_type
    n_conc = int(by.get("conceptual", {}).get("n", 0))
    n_sym = int(by.get("symbol_def", {}).get("n", 0))
    n_call = int(by.get("callers", {}).get("n", 0))
    n_abst = int(by.get("abstain_probe", {}).get("n", 0))
    rep.add(
        Metric(
            "localization recall@5 (conceptual)",
            summary.recall5,
            args.min_recall5,
            unit="%",
            n=n_conc,
        )
    )
    rep.add(
        Metric("localization MRR (conceptual)", summary.mrr, None, unit="%", n=n_conc, kind="info")
    )
    if n_sym:
        rep.add(
            Metric(
                "symbol definition exact@1",
                summary.symbol_exact,
                args.min_symbol_exact,
                unit="%",
                n=n_sym,
            )
        )
    if n_call:
        rep.add(
            Metric("caller-set F1", summary.callers_f1, args.min_callers_f1, unit="%", n=n_call)
        )
    if n_abst:
        wrong = int(summary.by_type["abstain_probe"].get("wrong_abstains_on_answerable") or 0)
        abstained = summary.abstain_recall > 0 or wrong > 0
        rep.add(
            Metric(
                "abstain precision (probes)",
                # a server that never abstains has no precision to gate: unmeasured, not 1.0
                summary.abstain_precision if abstained else None,
                args.min_abstain_precision,
                unit="%",
                n=n_abst,
                note="" if abstained else "the server abstained on nothing",
            )
        )
        rep.add(
            Metric(
                "abstain recall (probes)",
                summary.abstain_recall,
                None,
                unit="%",
                n=n_abst,
                kind="info",
            )
        )
    rep.add(
        Metric(
            "retrieval calls per answered question",
            econ["calls_per_answer"],
            args.max_calls_per_answer,
            higher_is_better=False,
            kind="economy",
            n=summary.n - summary.errors,
            note="HTTP attempts incl. retries",
        )
    )
    rep.add(
        Metric(
            "mean tokens_returned per answer",
            econ["mean_tokens"],
            args.max_mean_tokens,
            higher_is_better=False,
            kind="economy",
        )
    )
    rep.add(
        Metric(
            "p50 wall per retrieval",
            econ["p50_latency_ms"],
            None,
            higher_is_better=False,
            kind="economy",
            unit="ms",
        )
    )
    rep.add(Metric("errored questions (excluded)", float(summary.errors), None, kind="info"))
    rep.add(Metric("outcome reports posted", float(outcomes_posted), None, kind="info"))
    if summary.errors:
        rep.note(f"error kinds: {summary.error_kinds}")
    return rep


async def run(args: argparse.Namespace) -> int:
    questions = cq.load_questions(Path(args.dataset))
    if args.limit:
        questions = questions[: args.limit]
    if args.dry:
        pairs = [(q, cq.build_synthetic_retrieval(q)) for q in questions]
        summary = cq.summarize(pairs)
        econ = economy(summary, retrieval_attempts=len(pairs), latencies=[1.0] * len(pairs))
        rep = build_report(
            args, summary, econ, outcomes_posted=0, probe_line="probe skipped (--dry)"
        )
        rep.note("dry run: synthetic retrievals, no server")
        return finish(rep, args)

    client = make_client(args, timeout_s=30.0)
    async with client:
        probe = await client.probe("qa_agent")
        before = client.attempts
        pairs: list[tuple[cq.Question, cq.Retrieval]] = []
        traces: list[str] = []
        for q in questions:
            r, trace = await retrieve(client, q)
            pairs.append((q, r))
            traces.append(trace)
        retrieval_attempts = client.attempts - before
        summary = cq.summarize(pairs)
        latencies = [r.latency_ms for _, r in pairs if not r.error and r.latency_ms > 0]
        posted = 0
        if not args.no_outcomes:
            for (q, r), trace in zip(pairs, traces, strict=True):
                fb = outcome_for(q, r)
                if fb is None or not trace:
                    continue
                action, targets = fb
                out = await client.code_outcome(action, trace_run_id=trace, targets=targets)
                posted += int(out.ok)
        econ = economy(summary, retrieval_attempts=retrieval_attempts, latencies=latencies)
        rep = build_report(args, summary, econ, outcomes_posted=posted, probe_line=probe.summary())
        if not probe.ok:
            rep.note(f"probe: missing ops {probe.missing}")
        return finish(rep, args, client)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(
        ap, namespace_default="code-clickhouse", consumer_default="acceptance-code-agent"
    )
    ap.add_argument("--dataset", default=str(FIXTURE))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true", help="synthetic retrievals, no server")
    ap.add_argument("--min-recall5", type=float, default=0.75)
    ap.add_argument("--min-symbol-exact", type=float, default=0.90)
    ap.add_argument("--min-callers-f1", type=float, default=0.80)
    ap.add_argument("--min-abstain-precision", type=float, default=0.80)
    ap.add_argument("--max-calls-per-answer", type=float, default=1.2)
    ap.add_argument("--max-mean-tokens", type=float, default=4000.0)
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
