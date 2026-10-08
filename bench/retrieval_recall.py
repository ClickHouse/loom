"""Retrieval recall against a corpus that already exists.

Complements `bench/quality.py`, which seeds its own conversations and measures
extraction + retrieval together. This one seeds nothing: it scores
`memory.search` against a populated namespace using a labelled query -> gold
memory_key set. That separation matters — when quality.py drops you cannot tell
whether extraction stopped finding the fact or retrieval stopped ranking it.

It reports three things per run:

  recall@k  — fraction of queries whose gold key appears anywhere in the top k.
  MRR       — mean reciprocal rank of the first gold hit. Recall alone hides
              ranking regressions: moving every gold answer from rank 1 to
              rank 9 leaves recall@10 untouched and makes the product much
              worse, because agents read from the top.
  rank@1    — fraction ranked first, the number an agent actually experiences.

It also records `best_cos_rank`, the position of the highest-cosine hit. RRF
fusion is rank-based and does not promise rank 1 carries the best cosine; when
that gap is wide the abstain gate and the calibrator are being fed a row that
is not the best available match.

Defaults to `namespace_scope="this"`, and pins it even though the server default
is also "this": "all_mine" spans every namespace the token
can read and lets unrelated rows outrank gold — useless for a stable measurement.

Run:
    LOOM_API_KEY=... .venv/bin/python bench/retrieval_recall.py \
        --dataset /path/to/retrieval_qa.json \
        --base-url http://localhost:7777 --top-k 10 \
        --out /tmp/loom_retrieval_recall.csv

Exits non-zero when recall or MRR falls under `--min-recall` / `--min-mrr`, so
it can gate a change rather than merely describe one.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Keep in step with `_COSINE_PLANES` in server/observability/metrics.py.
COSINE_PLANES = frozenset({"fact", "chunk", "summary", "entity_summary", "observation", "anchor"})


@dataclass
class Row:
    query: str
    gold_keys: list[str]
    hit_rank: int | None = None  # 1-based rank of first gold hit; None = miss
    n_results: int = 0
    best_cos: float | None = None
    best_cos_rank: int | None = None
    abstain: bool = False
    abstain_code: str = ""
    returned: list[str] = field(default_factory=list)
    # Set when the request itself failed. An error is NOT a retrieval miss:
    # counting it as one silently biases recall downward and makes a flaky
    # network look like a quality regression, which is the opposite of what a
    # gating bench should do.
    error: str = ""
    # Populated from the `X-Trace-Id` response header, for dataset-run linking.
    trace_id: str = ""

    @property
    def recalled(self) -> bool:
        return self.hit_rank is not None

    @property
    def rr(self) -> float:
        return 1.0 / self.hit_rank if self.hit_rank else 0.0


def search(client: httpx.Client, base_url: str, token: str, body: dict) -> tuple[dict, str]:
    """Returns `(response_body, trace_id)`.

    The trace id comes from loom's `X-Trace-Id` response header and is the same
    hex Langfuse uses as its `traceId` — that is what lets a bench run link each
    query to the trace it produced instead of matching on timestamps.
    """
    r = client.post(
        f"{base_url.rstrip('/')}/v1/memory.search",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120.0,
    )
    r.raise_for_status()
    return r.json(), r.headers.get("X-Trace-Id", "")


def score_one(resp: dict, gold_keys: list[str]) -> Row:
    hits = resp.get("results") or []
    row = Row(query="", gold_keys=gold_keys, n_results=len(hits))
    row.abstain = bool(resp.get("abstain"))
    row.abstain_code = str(resp.get("abstain_code") or "")
    row.returned = [str(h.get("memory_key", "")) for h in hits]

    sims: list[float] = []
    for i, h in enumerate(hits, start=1):
        d = h.get("distance")
        # Only planes whose `distance` is a real cosine. Record-plane rows store
        # 1 - lexical_rank_score and computed/derived evidence rows hardcode 0.0,
        # so counting them made best_cos read a fabricated 1.000 at rank 1 —
        # hiding the very rank-0 gap this diagnostic exists to expose. Mirrors
        # _COSINE_PLANES in server/observability/metrics.py.
        if isinstance(d, (int, float)) and str(h.get("source") or "") in COSINE_PLANES:
            sims.append(1.0 - float(d))
        else:
            sims.append(float("nan"))
        # Substring match, matching quality.py's convention: gold keys are
        # recorded loosely enough to survive key-naming changes.
        key = str(h.get("memory_key", ""))
        if row.hit_rank is None and any(g in key for g in gold_keys):
            row.hit_rank = i

    clean = [(i, s) for i, s in enumerate(sims, start=1) if s == s]
    if clean:
        best_rank, best = max(clean, key=lambda p: p[1])
        row.best_cos, row.best_cos_rank = best, best_rank
    return row


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--dataset", required=True, help="labelled query -> memory_key set over an existing corpus"
    )
    p.add_argument("--base-url", default="http://localhost:7777")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--out", default="bench/retrieval_recall.csv")
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--namespace", default="", help="Override the dataset's namespace.")
    p.add_argument("--org", default="", help="Override the dataset's org.")
    p.add_argument(
        "--namespace-scope",
        default="this",
        choices=["this", "all_mine"],
        help="Default 'this' keeps the measurement stable; 'all_mine' reproduces "
        "the server default, where other namespaces compete for rank 1.",
    )
    p.add_argument("--recipe", default="", help="e.g. 'quality' to exercise assists.")
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra memory.search field, repeatable. VALUE is parsed as JSON when "
        "possible, else kept as a string — so `--param hyde=false` sends a bool "
        "and `--param retrieval_budget=fast` sends a string. Use this to attribute "
        "a quality change to a single retrieval leg without redeploying.",
    )
    p.add_argument("--min-recall", type=float, default=0.0)
    p.add_argument("--min-mrr", type=float, default=0.0)
    p.add_argument(
        "--max-error-frac",
        type=float,
        default=0.0,
        help="Largest fraction of queries allowed to error before the run fails. "
        "Default 0 — any error fails. Excluding errors from the ratios without "
        "this would let a mostly-failed run report high recall on its one "
        "surviving query and exit 0, greenlighting a regression.",
    )
    p.add_argument(
        "--langfuse-run",
        default="",
        help="Record this run against the Langfuse dataset of the same stem as "
        "--dataset, under this run name. Turns the pasted-stdout baseline in "
        "bench/README.md into a run-comparison view. Requires "
        "LOOM_LANGFUSE_ADMIN_TOKEN.",
    )
    p.add_argument(
        "--langfuse-results",
        default="",
        help="Also write the per-query run payload here as JSON, so the run can "
        "be pushed later (or from CI) via bench/langfuse_dataset.py run.",
    )
    args = p.parse_args()

    extra: dict[str, object] = {}
    for kv in args.param:
        if "=" not in kv:
            print(f"--param needs KEY=VALUE, got {kv!r}", file=sys.stderr)
            return 2
        k, _, v = kv.partition("=")
        try:
            extra[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            extra[k.strip()] = v

    if not args.token:
        print("set --token or LOOM_API_KEY", file=sys.stderr)
        return 2

    data = json.loads(Path(args.dataset).read_text())
    questions = data["questions"]
    ns = args.namespace or data.get("namespace", "")
    org = args.org or data.get("org", "")
    if not ns or not org:
        print("dataset needs org+namespace, or pass --org/--namespace", file=sys.stderr)
        return 2

    print(
        f"loom-retrieval-recall against {args.base_url}, ns={ns} org={org}, "
        f"scope={args.namespace_scope}, recipe={args.recipe or 'default'}, "
        f"extra={extra or '{}'}, "
        f"top_k={args.top_k}, questions={len(questions)}"
    )

    rows: list[Row] = []
    with httpx.Client() as client:
        for q in questions:
            body = {
                "org": org,
                "namespace": ns,
                "namespace_scope": args.namespace_scope,
                "query": q["query"],
                "top_k": args.top_k,
            }
            if args.recipe:
                body["recipe"] = args.recipe
            body.update(extra)
            try:
                resp, trace_id = search(client, args.base_url, args.token, body)
            except Exception as e:  # noqa: BLE001 — a bench must report, not crash
                print(f"  ERR   {q['query'][:46]:<48} {e}")
                rows.append(Row(query=q["query"], gold_keys=q["gold_keys"], error=str(e)[:200]))
                continue
            row = score_one(resp, q["gold_keys"])
            row.query = q["query"]
            row.trace_id = trace_id
            rows.append(row)
            mark = f"@{row.hit_rank}" if row.recalled else "MISS"
            gap = (
                f" best_cos@{row.best_cos_rank}"
                if row.best_cos_rank and row.best_cos_rank != 1
                else ""
            )
            print(
                f"  {mark:<5} {row.query[:46]:<48} "
                f"n={row.n_results:<3} best={row.best_cos:.3f}"
                f"{gap}{' ABSTAIN' if row.abstain else ''}"
                if row.best_cos is not None
                else f"  {mark:<5} {row.query[:46]:<48} n={row.n_results}"
            )

    errored = [r for r in rows if r.error]
    scored = [r for r in rows if not r.error]
    n = len(scored)
    recall = sum(r.recalled for r in scored) / n if n else 0.0
    mrr = sum(r.rr for r in scored) / n if n else 0.0
    at1 = sum(1 for r in scored if r.hit_rank == 1) / n if n else 0.0
    misranked = sum(1 for r in scored if r.best_cos_rank and r.best_cos_rank != 1)
    abstained = sum(1 for r in scored if r.abstain)

    print(f"\n{'metric':<28} value")
    print(f"{'recall@' + str(args.top_k):<28} {recall:.3f}  ({sum(r.recalled for r in rows)}/{n})")
    print(f"{'MRR':<28} {mrr:.3f}")
    print(f"{'gold ranked first':<28} {at1:.3f}")
    print(f"{'best-cosine not at rank 1':<28} {misranked}/{n}")
    print(f"{'abstained':<28} {abstained}/{n}")
    if errored:
        # Loud, and excluded from the ratios above — a partial run must never
        # be mistaken for a complete one.
        print(f"{'ERRORED (excluded)':<28} {len(errored)}/{len(rows)}")
        for r in errored:
            print(f"    {r.query[:60]}: {r.error[:90]}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "query",
                "gold_keys",
                "hit_rank",
                "reciprocal_rank",
                "n_results",
                "best_cos",
                "best_cos_rank",
                "abstain",
                "abstain_code",
                "error",
                "returned",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r.query,
                    "|".join(r.gold_keys),
                    r.hit_rank or "",
                    f"{r.rr:.4f}",
                    r.n_results,
                    f"{r.best_cos:.4f}" if r.best_cos is not None else "",
                    r.best_cos_rank or "",
                    r.abstain,
                    r.abstain_code,
                    r.error,
                    "|".join(r.returned),
                ]
            )
    print(f"\nwrote {out}")

    # Deliberately before the pass/fail gate below: a FAILING run is the one you
    # most want to open in the comparison view, and returning early would record
    # only the runs that passed.
    if args.langfuse_run or args.langfuse_results:
        payload = {
            "metadata": {
                "top_k": args.top_k,
                "recipe": args.recipe or "default",
                "namespace_scope": args.namespace_scope,
                "params": {k: str(v) for k, v in extra.items()},
                "recall": round(recall, 4),
                "mrr": round(mrr, 4),
                "rank_at_1": round(at1, 4),
                "errored": len(errored),
            },
            "items": [
                {
                    "query": r.query,
                    "trace_id": r.trace_id,
                    "scores": {
                        # Per-query, so the comparison view shows WHICH queries
                        # moved rather than only that the aggregate did.
                        "bench_recall": 1.0 if r.recalled else 0.0,
                        "bench_reciprocal_rank": round(r.rr, 4),
                        **(
                            {"bench_top_similarity": round(r.best_cos, 4)}
                            if r.best_cos is not None
                            else {}
                        ),
                    },
                }
                for r in rows
                if r.trace_id and not r.error
            ],
        }
        if args.langfuse_results:
            Path(args.langfuse_results).write_text(json.dumps(payload, indent=2))
            print(f"wrote {args.langfuse_results}")
        if args.langfuse_run:
            missing = [r.query for r in rows if not r.error and not r.trace_id]
            if missing:
                # Loud: a silently-partial run recorded as complete is exactly the
                # failure this whole exercise keeps finding.
                print(
                    f"WARNING {len(missing)}/{len(rows)} queries returned no "
                    f"X-Trace-Id and are NOT in the recorded run — is the server "
                    f"older than the header?"
                )
            try:
                # Run as a script, `sys.path[0]` is `bench/`, not the repo root,
                # so the package import fails with ModuleNotFoundError. Add the
                # root rather than importing the sibling bare, so the same line
                # works under `python bench/retrieval_recall.py` and
                # `python -m bench.retrieval_recall`.
                _root = str(Path(__file__).resolve().parent.parent)
                if _root not in sys.path:
                    sys.path.insert(0, _root)
                from bench.langfuse_dataset import record_run

                record_run(
                    dataset_name=Path(args.dataset).stem,
                    run_name=args.langfuse_run,
                    items=payload["items"],
                    run_metadata=payload["metadata"],
                )
            except SystemExit as e:
                print(f"langfuse run not recorded: {e}")
            except Exception as e:  # noqa: BLE001 — never fail the bench on this
                print(f"langfuse run not recorded: {type(e).__name__}: {e}")

    failed = False
    if not scored:
        print("FAIL every query errored — no measurement was taken")
        return 1
    err_frac = len(errored) / len(rows) if rows else 0.0
    if err_frac > args.max_error_frac:
        # Errors are excluded from the ratios, so without this a run that was
        # 83% failures prints recall 1.000 (1/1) and exits 0.
        print(
            f"FAIL {len(errored)}/{len(rows)} queries errored "
            f"({err_frac:.0%} > --max-error-frac {args.max_error_frac:.0%}) — "
            f"the metrics above describe only the {len(scored)} that ran"
        )
        failed = True
    if recall < args.min_recall:
        print(f"FAIL recall@{args.top_k} {recall:.3f} < --min-recall {args.min_recall}")
        failed = True
    if mrr < args.min_mrr:
        print(f"FAIL MRR {mrr:.3f} < --min-mrr {args.min_mrr}")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
