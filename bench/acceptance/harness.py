"""Acceptance bench — the evaluation harness: bursty,
thousands of namespaces, most of a deployment's LLM spend.

Precision first: judged accuracy on a LoCoMo slice answered by `memory.ask`,
through `bench/locomo_qa.py --reader ask` with its pinned judge. Two slices,
each with the floor this build measured, rounded down — a floor a slice
cannot fail is not a gate:

  --slice conv26-30      the first conversation's first 30 questions
                         (default; reads 28–29/30, floor 0.85)
  --slice stratified-100 the first 10 questions of each of the 10
                         conversations (100; the board's level, floor 0.65)

`--conversations / --questions / --min-accuracy` override a preset. Economy second: the
cost per answer read from the deployment's ledger (`cost.summary` on the
run's namespace — what loom spent upstream on `memory.ask` and on the
`set_from_messages` ingest; the bench's own judge spend is outside the
ledger and is not counted). The run's namespaces are erased by explicit
list at the end.

Needs the dataset (`--dataset`, third-party: snap-research/locomo
`locomo10.json`) and OPENAI_API_KEY for the judge.

    python bench/acceptance/harness.py --base-url http://127.0.0.1:7893 \\
        --token-file bench.token --dataset bench/datasets/locomo10.json --questions 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "sdk" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bench.acceptance._common import (  # noqa: E402
    ROOT,
    Metric,
    Report,
    add_common_args,
    erase_namespaces,
    erasure_note,
    finish,
    ledger_cost_by_op,
    make_client,
    nonce,
    ratio,
    resolve_token,
)


def locomo_command(args: argparse.Namespace, out_csv: Path, prefix: str) -> list[str]:
    cmd = [
        sys.executable,
        str(ROOT / "bench" / "locomo_qa.py"),
        "--dataset",
        args.dataset,
        "--base-url",
        args.base_url,
        "--org",
        args.org,
        "--namespace-prefix",
        prefix,
        "--limit-conversations",
        str(args.conversations),
        "--limit-questions",
        str(args.questions),
        "--reader",
        "ask",
        "--qa-concurrency",
        str(args.qa_concurrency),
        "--keep-namespaces",
        "--no-footprint",
        "--out",
        str(out_csv),
    ]
    if args.categories:
        cmd += ["--categories", args.categories]
    if getattr(args, "ask_json", ""):
        cmd += ["--ask-json", args.ask_json]
    return cmd


SLICES: dict[str, dict[str, Any]] = {
    # floors are the measured levels rounded down
    "conv26-30": {"conversations": 1, "questions": 30, "min_accuracy": 0.85},
    "stratified-100": {"conversations": 10, "questions": 10, "min_accuracy": 0.65},
}


def apply_slice(args: argparse.Namespace) -> argparse.Namespace:
    """Fill conversations / questions / min_accuracy from the slice preset
    where the caller left them unset (pure)."""
    preset = SLICES[args.slice]
    for k, v in preset.items():
        if getattr(args, k, None) is None:
            setattr(args, k, v)
    return args


def judged_rollup(rows: list[dict[str, str]]) -> dict[str, float | None]:
    """Judged accuracy over the answered questions (ERROR rows excluded),
    abstain rate, and the per-category counts (pure)."""
    judged = [r for r in rows if r.get("label") in ("CORRECT", "WRONG")]
    correct = sum(1 for r in judged if r["label"] == "CORRECT")
    abstained = sum(1 for r in judged if str(r.get("abstained", "")).lower() in ("true", "1"))
    errors = sum(1 for r in rows if r.get("label") == "ERROR")
    by_cat: dict[str, list[int]] = {}
    for r in judged:
        c = by_cat.setdefault(str(r.get("category_name") or r.get("category") or "?"), [0, 0])
        c[1] += 1
        c[0] += int(r["label"] == "CORRECT")
    return {
        "accuracy": ratio(correct, len(judged)),
        "n": float(len(judged)),
        "correct": float(correct),
        "abstain_rate": ratio(abstained, len(judged)),
        "errors": float(errors),
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},  # type: ignore[dict-item]
    }


def cost_rollup(
    cost: dict[str, dict[str, float]], *, correct: float, answered: float
) -> dict[str, float | None]:
    ask = cost.get("memory.ask", {})
    ingest = cost.get("memory.set_from_messages", {})
    total = sum(v.get("cost_usd", 0.0) for v in cost.values())
    return {
        "usd_per_ask": ratio(ask.get("cost_usd", 0.0), ask.get("ops", 0.0)),
        "usd_per_correct_answer": ratio(ask.get("cost_usd", 0.0), correct),
        "usd_ingest_per_session": ratio(ingest.get("cost_usd", 0.0), ingest.get("ops", 0.0)),
        "usd_total": total,
        "usd_total_per_answer": ratio(total, answered),
        "ask_p50_ms": ask.get("latency_p50_ms") or None,
        "ask_ops": ask.get("ops", 0.0),
        "ingest_ops": ingest.get("ops", 0.0),
    }


def build_report(
    args: argparse.Namespace,
    judged: dict[str, Any],
    cost: dict[str, float | None],
    *,
    probe_line: str,
) -> Report:
    rep = Report(
        bench="harness",
        archetype="evaluation harness (bursty, many namespaces)",
        base_url=args.base_url,
        consumer=args.consumer,
        probe=probe_line,
    )
    n = int(judged.get("n") or 0)
    rep.add(
        Metric(
            "judged accuracy (memory.ask, LoCoMo slice)",
            judged.get("accuracy"),
            args.min_accuracy,
            unit="%",
            n=n,
        )
    )
    rep.add(Metric("abstain rate", judged.get("abstain_rate"), None, unit="%", n=n, kind="info"))
    rep.add(Metric("errored questions (excluded)", judged.get("errors"), None, kind="info"))
    rep.add(
        Metric(
            "memory.ask $ per answer (ledger)",
            cost["usd_per_ask"],
            args.max_usd_per_answer,
            higher_is_better=False,
            kind="economy",
            unit="$",
            n=int(cost.get("ask_ops") or 0),
        )
    )
    rep.add(
        Metric(
            "memory.ask $ per CORRECT answer (ledger)",
            cost["usd_per_correct_answer"],
            None,
            higher_is_better=False,
            kind="economy",
            unit="$",
        )
    )
    rep.add(
        Metric(
            "ingest $ per session (ledger)",
            cost["usd_ingest_per_session"],
            None,
            higher_is_better=False,
            kind="economy",
            unit="$",
            n=int(cost.get("ingest_ops") or 0),
        )
    )
    rep.add(
        Metric(
            "ledger $ total for the run",
            cost["usd_total"],
            None,
            higher_is_better=False,
            kind="economy",
            unit="$",
        )
    )
    rep.add(
        Metric(
            "memory.ask p50 wall (ledger, max over namespaces)",
            cost["ask_p50_ms"],
            None,
            higher_is_better=False,
            kind="economy",
            unit="ms",
        )
    )
    if judged.get("by_category"):
        rep.note(f"by category (correct/n): {judged['by_category']}")
    if n and n < 30:
        rep.smoke = True
    rep.note("judge spend (the bench's own reader/judge) is outside the ledger and not counted")
    return rep


async def run(args: argparse.Namespace) -> int:
    token = resolve_token(args)
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("BENCH_OPENAI_API_KEY")):
        print("OPENAI_API_KEY is required for the judge", file=sys.stderr)
        return 2
    prefix = f"{args.namespace}-{nonce()}"
    out_csv = Path(args.out or f"bench/acceptance_locomo_{nonce()}.csv")
    client = make_client(args, namespace=prefix, agent="-", timeout_s=30.0)
    async with client:
        probe = await client.probe("harness")
        cmd = locomo_command(args, out_csv, prefix)
        # the bearer rides the child's environment, not its argv (visible in ps)
        env = {**os.environ, "LOOM_API_KEY": token}
        out_csv.unlink(missing_ok=True)  # a stale CSV must not be scored as this run's
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout_s,
                env=env,
            )
            failure = "" if proc.returncode == 0 else f"locomo_qa exited {proc.returncode}"
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
        except subprocess.TimeoutExpired:
            failure, tail = f"locomo_qa timed out after {args.timeout_s} s", []
        if failure or not out_csv.exists():
            rep_fail = Report(
                bench="harness",
                archetype="evaluation harness",
                base_url=args.base_url,
                consumer=args.consumer,
                probe=probe.summary(),
            )
            rep_fail.note(
                (failure or "locomo_qa produced no CSV")
                + (": " + " | ".join(tail) if tail else "")
                + f"; namespaces {prefix}-* retire by idleness (kind = bench)"
            )
            return finish(rep_fail, args, client)
        with out_csv.open() as f:
            rows = list(csv.DictReader(f))
        judged = judged_rollup(rows)
        summary_path = out_csv.with_suffix(".summary.json")
        namespaces: list[str] = []
        if summary_path.exists():
            summary = json.loads(summary_path.read_text())
            namespaces = sorted({str(v) for v in (summary.get("namespaces") or {}).values()})
        if not namespaces:
            namespaces = sorted({str(r.get("namespace")) for r in rows if r.get("namespace")})
        cost_by_op = await ledger_cost_by_op(client, namespaces)
        cost = cost_rollup(
            cost_by_op,
            correct=float(judged.get("correct") or 0.0),
            answered=float(judged.get("n") or 0.0),
        )
        rep = build_report(args, judged, cost, probe_line=probe.summary())
        if not probe.ok:
            rep.note(f"probe: missing ops {probe.missing}")
        rep.note(f"namespaces: {namespaces}; csv {out_csv}")
        if args.keep_namespaces:
            rep.note("namespaces kept (--keep-namespaces)")
        else:
            erased = await erase_namespaces(client, namespaces)
            rep.note(erasure_note(erased, len(set(namespaces))))
        return finish(rep, args, client)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(
        ap, namespace_default="bench-acc-harness", consumer_default="acceptance-harness"
    )
    ap.add_argument("--dataset", default=str(ROOT / "bench" / "datasets" / "locomo10.json"))
    ap.add_argument("--slice", choices=sorted(SLICES), default="conv26-30")
    ap.add_argument("--conversations", type=int, default=None, help="preset by --slice")
    ap.add_argument("--questions", type=int, default=None, help="per conversation; preset")
    ap.add_argument(
        "--ask-json",
        default='{"reinforce": false}',
        help="JSON merged into every memory.ask body; the default makes the bench an "
        "evaluation read that credits nothing",
    )
    ap.add_argument(
        "--categories", default="", help="comma list of LoCoMo categories 1-5; default all"
    )
    ap.add_argument("--qa-concurrency", type=int, default=4)
    ap.add_argument("--timeout-s", type=int, default=5400)
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-namespaces", action="store_true")
    ap.add_argument("--min-accuracy", type=float, default=None, help="preset by --slice")
    ap.add_argument("--max-usd-per-answer", type=float, default=0.10)
    args = ap.parse_args(argv)
    apply_slice(args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
