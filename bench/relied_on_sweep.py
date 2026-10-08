"""Sweep ranking arms against a relied-on set and ship the paired bound.

`bench/relied_on_set.py` writes (query, gold_keys) pairs labelled by use.
This runs every arm — a JSON dict of `memory.search` field overrides, the
same shape `retrieval_recall.py --param` takes —
over the same questions, scores each question per arm (reciprocal rank and
hit@k of the first gold key, `retrieval_recall.score_one`), and reports the
PAIRED bootstrap delta of every arm against the first one (`bench/bootstrap`):
the 2.5th percentile of the mean delta is the number a
default moves on, not the point estimate.

    python bench/relied_on_sweep.py \\
        --dataset bench/datasets/relied_on/<org>__<namespace>.json \\
        --base-url $LOOM_BASE_URL --token $TOK --top-k 10 \\
        --arms '[{}, {"reinforcement_weight": 0}, {"personalization": "none"}]'

Questions from the same trace agent (or the same `cluster` field, when the
dataset carries one) are resampled together so correlated questions do not
narrow the interval. `--min-lb` turns the sweep into a gate on one arm.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench._gate import (
    add_gate_arguments,
    gate_from_args,
    identical_share,
    measurement_fields,
)
from bench.bootstrap import BootResult, paired_bootstrap
from bench.retrieval_recall import Row, score_one

PROPENSITY_FLOOR = 0.05  # clip so one rarely-examined rank cannot dominate a question


@dataclass
class ArmScores:
    arm: dict[str, Any]
    rows: list[Row]

    @property
    def mrr(self) -> float:
        return sum(r.rr for r in self.rows) / len(self.rows) if self.rows else 0.0

    def recall_at(self, k: int) -> float:
        if not self.rows:
            return 0.0
        hits = sum(1 for r in self.rows if r.hit_rank is not None and r.hit_rank <= k)
        return hits / len(self.rows)


def per_question(rows: list[Row], *, k: int) -> tuple[list[float], list[float]]:
    """(reciprocal ranks, hit@k as 0/1) in question order."""
    rr = [r.rr for r in rows]
    hit = [1.0 if (r.hit_rank is not None and r.hit_rank <= k) else 0.0 for r in rows]
    return rr, hit


def ips_recall(
    row: Row, *, k: int, shown_ranks: dict[str, int], propensity: dict[str, float]
) -> float:
    """Inverse-propensity-weighted recall@k of the gold set. Each gold key
    weighs 1/propensity(rank it was shown at by the labelling retrieval),
    normalised over the question's gold, so a key the reader cited from rank
    14 (rarely examined) counts more than one cited from rank 1, which any
    ranking resembling the labeller would have
    surfaced anyway. Gold keys without a shown rank weigh as rank 1. A gold
    key counts as retrieved when the arm returns it (substring match, as
    `score_one`) within the first k results."""
    if not row.gold_keys:
        return 0.0
    top = [str(x) for x in row.returned[:k]]
    p1 = float(propensity.get("1", 1.0) or 1.0)
    weights: list[float] = []
    hits: list[float] = []
    for g in row.gold_keys:
        r = shown_ranks.get(g)
        p = float(propensity.get(str(r), p1) if r else p1) if propensity else 1.0
        w = 1.0 / max(p, PROPENSITY_FLOOR)
        weights.append(w)
        hits.append(1.0 if any(g in key for key in top) else 0.0)
    z = sum(weights)
    return sum(w * h for w, h in zip(weights, hits, strict=True)) / z if z else 0.0


def compare(
    base: ArmScores,
    other: ArmScores,
    *,
    k: int,
    clusters: list[str] | None,
    resamples: int = 10_000,
    seed: int = 7,
    questions: list[dict[str, Any]] | None = None,
    propensity: dict[str, float] | None = None,
) -> dict[str, BootResult]:
    """Paired bootstrap of `other - base` on MRR, hit@k and, when the dataset
    carries shown ranks, IPS-weighted gold recall@k. Pure."""
    if len(base.rows) != len(other.rows):
        raise ValueError("arms scored different question counts")
    b_rr, b_hit = per_question(base.rows, k=k)
    o_rr, o_hit = per_question(other.rows, k=k)
    out = {
        "mrr": paired_bootstrap(
            [o - b for b, o in zip(b_rr, o_rr, strict=True)],
            clusters,
            resamples=resamples,
            seed=seed,
        ),
        f"hit@{k}": paired_bootstrap(
            [o - b for b, o in zip(b_hit, o_hit, strict=True)],
            clusters,
            resamples=resamples,
            seed=seed,
        ),
    }
    if questions and propensity:
        deltas = []
        for q, rb, ro in zip(questions, base.rows, other.rows, strict=True):
            sr = {str(a): int(b) for a, b in (q.get("gold_shown_ranks") or {}).items()}
            deltas.append(
                ips_recall(ro, k=k, shown_ranks=sr, propensity=propensity)
                - ips_recall(rb, k=k, shown_ranks=sr, propensity=propensity)
            )
        out[f"ips_recall@{k}"] = paired_bootstrap(deltas, clusters, resamples=resamples, seed=seed)
    return out


_NO_CLUSTER = {"", "*", "-"}


def cluster_keys(questions: list[dict[str, Any]]) -> list[str] | None:
    """A dataset `cluster` field wins; else the trace agent recorded in the
    relied-on note (`agent=<name>`). None when nothing groups them, when the
    only label is the read-all agent (`*`) or unset (`-`), or when every
    question falls in ONE cluster — resampling a single cluster would return
    the same set every draw and print a zero-width interval."""
    out: list[str] = []
    for q in questions:
        c = str(q.get("cluster") or "")
        if not c:
            note = str(q.get("note") or "")
            for part in note.split(";"):
                part = part.strip()
                if part.startswith("agent="):
                    c = part[len("agent=") :]
        out.append("" if c in _NO_CLUSTER else c)
    distinct = {c for c in out if c}
    if len(distinct) < 2:
        return None
    # Unlabelled questions stay their own cluster.
    return [c or f"q{i}" for i, c in enumerate(out)]


def hit_labels(scores: ArmScores, k: int) -> dict[str, list[str]]:
    """hit@k per question as a CORRECT/WRONG label for the accuracy gate."""
    return {
        str(i): ["CORRECT" if (r.hit_rank is not None and r.hit_rank <= k) else "WRONG"]
        for i, r in enumerate(scores.rows)
    }


def _search(client: Any, base_url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    import time

    for attempt in range(6):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = client.post(
            base_url.rstrip("/") + "/v1/memory.search", json=body, headers=headers, timeout=120.0
        )
        if r.status_code == 429 and attempt < 5:
            time.sleep(float(r.headers.get("Retry-After") or 0) or 1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def run_arm(
    client: Any,
    *,
    base_url: str,
    token: str,
    dataset: dict[str, Any],
    arm: dict[str, Any],
    top_k: int,
    namespace_scope: str,
    credit: bool = False,
) -> ArmScores:
    rows: list[Row] = []
    for q in dataset["questions"]:
        body = {
            "org": dataset["org"],
            "namespace": dataset["namespace"],
            "agent": "*",
            "query": q["query"],
            "top_k": top_k,
            "namespace_scope": namespace_scope,
            # An evaluation read credits no recall unless --credit.
            **measurement_fields("search", credit=credit),
            **arm,
        }
        try:
            resp = _search(client, base_url, token, body)
            row = score_one(resp, list(q["gold_keys"]))
        except Exception as e:  # noqa: BLE001 — a failed question is a row, not a crash
            row = Row(query="", gold_keys=list(q["gold_keys"]))
            row.error = str(e)[:120]
        row.query = q["query"]
        rows.append(row)
    return ArmScores(arm=arm, rows=rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dataset", required=True)
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--namespace-scope", default="this", choices=["this", "all_mine"])
    p.add_argument(
        "--arms",
        default="[{}]",
        help="JSON list of memory.search overrides; the first is the baseline.",
    )
    p.add_argument("--resamples", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument(
        "--min-lb",
        type=float,
        default=None,
        help="Gate: exit 1 unless the LAST arm's MRR lower bound beats this.",
    )
    p.add_argument("--out", default="")
    add_gate_arguments(p)  # --repeats / --gate* / --credit / --judge-calibration
    args = p.parse_args(argv)

    import httpx

    dataset = json.loads(Path(args.dataset).read_text())
    arms: list[dict[str, Any]] = json.loads(args.arms)
    if not arms:
        print("--arms needs at least the baseline {}", file=sys.stderr)
        return 2
    qs = dataset["questions"]
    clusters = cluster_keys(qs)
    shapes = dataset.get("_label_shapes") or []
    propensity: dict[str, float] = dict(dataset.get("_propensity_by_rank") or {})
    print(
        f"relied-on sweep: {dataset['org']}/{dataset['namespace']} questions={len(qs)} "
        f"top_k={args.top_k} arms={len(arms)} built={dataset.get('_built', {}).get('at', '?')} "
        f"label_shapes={len(shapes)}"
    )
    if len(shapes) == 1:
        print(
            "  NOTE: every label came from ONE retrieval shape; an arm that reproduces that "
            "shape's ordering is favoured by construction (see the dataset _caveat)."
        )
    if propensity:
        lo = min(propensity.items(), key=lambda kv: int(kv[0]))
        hi = max(propensity.items(), key=lambda kv: int(kv[0]))
        print(
            f"  label propensity: rank {lo[0]} {lo[1]:.2f} … rank {hi[0]} {hi[1]:.2f} "
            f"(ips_recall@{args.top_k} weights gold by 1/propensity)"
        )
    scored: list[ArmScores] = []
    identical_rates: list[float] = []
    repeats = max(1, int(getattr(args, "repeats", 1) or 1))
    with httpx.Client() as client:
        for arm in arms:
            runs = [
                run_arm(
                    client,
                    base_url=args.base_url,
                    token=args.token,
                    dataset=dataset,
                    arm=arm,
                    top_k=args.top_k,
                    namespace_scope=args.namespace_scope,
                    credit=bool(getattr(args, "credit", False)),
                )
                for _ in range(repeats)
            ]
            s = runs[0]
            errs = sum(1 for r in s.rows if r.error)
            # Retrieval on a fixed index is deterministic, so
            # --repeats here is the PIN CHECK — the share of questions whose
            # returned list was identical on every repeat — not a vote.
            ident = sum(
                identical_share([run.rows[i].returned for run in runs]) >= 1.0
                for i in range(len(s.rows))
            ) / max(1, len(s.rows))
            identical_rates.append(round(ident, 4))
            print(
                f"  arm {json.dumps(arm):<48} MRR {s.mrr:.3f}  "
                f"hit@{args.top_k} {s.recall_at(args.top_k):.3f}  errors {errs}"
                + (f"  identical over {repeats} repeats {ident:.3f}" if repeats > 1 else "")
            )
            scored.append(s)
    report: dict[str, Any] = {
        "dataset": args.dataset,
        "top_k": args.top_k,
        "n": len(qs),
        "arms": [],
    }
    base = scored[0]
    last_lb = None
    for s in scored:
        entry: dict[str, Any] = {
            "arm": s.arm,
            "mrr": round(s.mrr, 4),
            f"hit@{args.top_k}": round(s.recall_at(args.top_k), 4),
            # per-question rows so the bound can be recomputed offline
            "rows": [
                {
                    "query": r.query,
                    "hit_rank": r.hit_rank,
                    "rr": round(r.rr, 4),
                    "returned": r.returned,
                    "error": r.error,
                }
                for r in s.rows
            ],
        }
        if s is not base:
            res = compare(
                base,
                s,
                k=args.top_k,
                clusters=clusters,
                resamples=args.resamples,
                seed=args.seed,
                questions=qs,
                propensity=propensity,
            )
            print(f"\n# {json.dumps(s.arm)} vs baseline")
            for metric, br in res.items():
                print(f"  {metric:<8} {br.line(scale=1.0, digits=3)}")
            entry["vs_baseline"] = {
                m: {
                    "mean": br.mean,
                    "lb": br.lb,
                    "ub": br.ub,
                    "wins": br.wins,
                    "ties": br.ties,
                    "losses": br.losses,
                }
                for m, br in res.items()
            }
            last_lb = res["mrr"].lb
            if getattr(args, "gate", False):
                # Gate verdict on hit@k (a 0/1 label per question).
                cl = {str(i): c for i, c in enumerate(clusters)} if clusters else None
                g = gate_from_args(
                    args,
                    hit_labels(base, args.top_k),
                    hit_labels(s, args.top_k),
                    clusters=cl,
                    seed=args.seed,
                )
                entry["gate"] = g.as_dict()
                for line in g.lines():
                    print("  " + line)
        if repeats > 1:
            entry["identical_over_repeats"] = identical_rates[len(report["arms"])]
        report["arms"].append(entry)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
        print(f"wrote {args.out}")
    if args.min_lb is not None and (last_lb is None or last_lb <= args.min_lb):
        print(f"GATE: last arm MRR lower bound {last_lb} <= {args.min_lb}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
