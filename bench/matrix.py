"""Bench matrix runner — sweep (search_mode × rerank × pool_unmatched)
on LongMemEval and emit a summary table.

Wraps `bench/longmemeval.py` as a subprocess per config so each run
gets a clean event loop + the per-item CSV columns (search_mode,
rerank, pool_unmatched, search_latency_ms) are populated correctly
for downstream slice-and-dice.

Usage:
    python bench/matrix.py --limit 30 --out bench/MATRIX_RESULTS.md

  Smoke run (3 items per config):
    python bench/matrix.py --limit 3

Each subprocess inherits OPENAI_API_KEY + LOOM_API_KEY from the
parent. Hits localhost:7777 by default; point at a different
deployment with --base-url. `--dataset` is passed through, and the
report names the file every config ran on.

Output: bench/MATRIX_RESULTS.md (markdown table: config × recall,
judge, p50/p99 search latency, exit code). Per-config CSVs land in
bench/matrix_runs/<config>.csv for deeper inspection. Exits 1 when any
config's run failed or wrote no rows.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LONGMEMEVAL = Path(__file__).resolve().parent / "longmemeval.py"
DEFAULT_DATASET = "bench/datasets/longmemeval_s_cleaned.json"

# Per-config knobs. Drop a row to skip; add a row to extend. The
# matrix is the cross product; total configs = len(CONFIGS).
CONFIGS: list[dict[str, str]] = [
    # (mode, rerank, pool_unmatched) — production posture first.
    {"search_mode": "bm25_hybrid", "rerank": "builtin:openai", "pool_unmatched": "120"},
    # Ablations: each leg dropped in turn.
    {"search_mode": "bm25_hybrid", "rerank": "none", "pool_unmatched": "120"},
    {"search_mode": "semantic", "rerank": "none", "pool_unmatched": "120"},
    {"search_mode": "lexical", "rerank": "none", "pool_unmatched": "120"},
    {"search_mode": "hybrid", "rerank": "none", "pool_unmatched": "120"},
    # Pool-width ablation on production posture.
    {"search_mode": "bm25_hybrid", "rerank": "builtin:openai", "pool_unmatched": "60"},
    {"search_mode": "bm25_hybrid", "rerank": "builtin:openai", "pool_unmatched": "240"},
]


@dataclass
class ConfigResult:
    label: str
    search_mode: str
    rerank: str
    pool_unmatched: int
    n: int
    recall_n: int
    judge_n: int
    p50_latency_ms: int
    p99_latency_ms: int
    csv_path: Path
    returncode: int = 0

    @property
    def failed(self) -> bool:
        return self.returncode != 0 or self.n == 0


def _run_one(cfg: dict[str, str], args: argparse.Namespace, run_dir: Path) -> ConfigResult:
    label = f"{cfg['search_mode']}__{cfg['rerank'].replace(':', '_')}__pu{cfg['pool_unmatched']}"
    out_csv = run_dir / f"{label}.csv"
    cmd = [
        sys.executable,
        str(LONGMEMEVAL),
        "--base-url",
        args.base_url,
        "--dataset",
        args.dataset,
        "--limit",
        str(args.limit),
        "--seed",
        str(args.seed),
        "--top-k",
        str(args.top_k),
        "--concurrency",
        str(args.concurrency),
        "--search-mode",
        cfg["search_mode"],
        "--rerank",
        cfg["rerank"],
        "--pool-unmatched",
        cfg["pool_unmatched"],
        "--out",
        str(out_csv),
    ]
    if args.shuffle:
        cmd.append("--shuffle")
    if args.answer_judge:
        cmd.append("--answer-judge")
    if args.judge_model:
        cmd.extend(["--judge-model", args.judge_model])
    if args.reader_model:
        cmd.extend(["--reader-model", args.reader_model])
    print(f"\n=== running config: {label} ===", flush=True)
    print("  " + " ".join(cmd), flush=True)

    rc = subprocess.call(cmd)
    if rc != 0:
        # Continue to the next config; the row and the exit code carry the failure.
        print(f"  ! config {label} exited with rc={rc}", file=sys.stderr)
    return _summarize_csv(label, cfg, out_csv, rc)


def _summarize_csv(
    label: str, cfg: dict[str, str], csv_path: Path, returncode: int = 0
) -> ConfigResult:
    n = recall_n = judge_n = 0
    latencies: list[int] = []
    if csv_path.exists():
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                n += 1
                if row.get("recalled") == "1":
                    recall_n += 1
                if row.get("judge_label", "").upper() == "CORRECT":
                    judge_n += 1
                try:
                    latencies.append(int(row.get("search_latency_ms") or 0))
                except ValueError:
                    pass
    latencies.sort()

    def _pct(p: float) -> int:
        if not latencies:
            return 0
        i = max(0, min(len(latencies) - 1, int(p * (len(latencies) - 1))))
        return latencies[i]

    return ConfigResult(
        label=label,
        search_mode=cfg["search_mode"],
        rerank=cfg["rerank"],
        pool_unmatched=int(cfg["pool_unmatched"]),
        n=n,
        recall_n=recall_n,
        judge_n=judge_n,
        p50_latency_ms=_pct(0.50),
        p99_latency_ms=_pct(0.99),
        csv_path=csv_path,
        returncode=returncode,
    )


def _write_markdown(results: list[ConfigResult], out_path: Path, args: argparse.Namespace) -> None:
    lines: list[str] = []
    lines.append("# Bench matrix results")
    lines.append("")
    lines.append(
        f"- Dataset: LongMemEval (`{args.dataset}`), "
        f"N={args.limit}, seed={args.seed}, top_k={args.top_k}"
    )
    lines.append(f"- Base URL: `{args.base_url}`")
    judge = f"on ({args.judge_model or 'official default'})" if args.answer_judge else "off"
    lines.append(f"- Judge: {judge}")
    if args.answer_judge:
        lines.append(f"- Reader: {args.reader_model or 'pinned default'}")
    lines.append("")
    lines.append("| Config | N | Recall@K | Judge | p50 lat | p99 lat | rc |")
    lines.append("|---|---:|---|---|---:|---:|---:|")
    for r in results:
        recall_pct = f"{(100.0 * r.recall_n / r.n):.1f}%" if r.n else "—"
        judge_pct = f"{(100.0 * r.judge_n / r.n):.1f}%" if (r.n and args.answer_judge) else "—"
        lines.append(
            f"| {r.search_mode} / {r.rerank} / pu={r.pool_unmatched} "
            f"| {r.n} | {r.recall_n}/{r.n} ({recall_pct}) "
            f"| {r.judge_n}/{r.n} ({judge_pct}) "
            f"| {r.p50_latency_ms}ms | {r.p99_latency_ms}ms | {r.returncode} |"
        )
    lines.append("")
    lines.append("Per-config raw CSVs: `bench/matrix_runs/<config>.csv`")
    out_path.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {out_path}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:7777")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--top-k", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--shuffle", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--answer-judge", action=argparse.BooleanOptionalAction, default=True)
    # Reader/judge are pinned inside longmemeval.py (the instrument must not
    # follow LOOM_LLM_MODEL); forward overrides only when explicitly set.
    p.add_argument("--judge-model", default=os.environ.get("BENCH_JUDGE_MODEL"))
    p.add_argument("--reader-model", default=os.environ.get("BENCH_READER_MODEL"))
    p.add_argument("--out", default="bench/MATRIX_RESULTS.md")
    args = p.parse_args(argv)

    run_dir = Path("bench/matrix_runs")
    run_dir.mkdir(parents=True, exist_ok=True)
    results = [_run_one(cfg, args, run_dir) for cfg in CONFIGS]
    _write_markdown(results, Path(args.out), args)
    failed = [r.label for r in results if r.failed]
    if failed:
        print(f"{len(failed)} config(s) failed or wrote no rows: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
