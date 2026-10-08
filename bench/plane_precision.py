"""Per-plane precision: which retrieval plane's hits come from the evidence?

Summaries can rank below raw turns as a memory unit: they drop the sentence that
answers. Loom fuses three planes into one top-k — extracted facts, raw chunks, session
digests — and this measures their share of the evidence, not only their share of the
slots. For a LoCoMo conversation whose rows carry `session_id`, this script searches
every question and, per plane (`hit.source`), counts hits, hits whose session is one of
the question's gold evidence sessions, the first gold rank, and whether the gold session
would still be in the top-k with that plane's hits removed.

    python bench/plane_precision.py --base-url http://localhost:7803 \\
        --namespace <namespace> --agent locomo-bench --sample-id conv-26

Prints one row per plane and writes `--out`. Compare two server settings by
running once against an isolated server under each and diffing the JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.locomo_qa import evidence_sessions_cited

_SESSION_NO_RE = re.compile(r"session_(\d+)$")


def session_no(hit: dict[str, Any]) -> int | None:
    m = _SESSION_NO_RE.search(str(hit.get("session_id") or ""))
    return int(m.group(1)) if m else None


def plane_of(hit: dict[str, Any]) -> str:
    return str(hit.get("source") or "fact")


def tally(hits_per_question: list[tuple[list[dict[str, Any]], set[int]]], k: int) -> dict[str, Any]:
    """Aggregate per-plane counts over (hits, gold_sessions) pairs."""
    total: Counter[str] = Counter()
    gold: Counter[str] = Counter()
    first_rank_sum: Counter[str] = Counter()
    first_rank_n: Counter[str] = Counter()
    questions_with: Counter[str] = Counter()
    hit_all = 0
    hit_without: Counter[str] = Counter()
    planes: set[str] = set()
    for hits, gold_sessions in hits_per_question:
        top = hits[:k]
        seen: set[str] = set()
        firsts: dict[str, int] = {}
        for rank, h in enumerate(top, start=1):
            p = plane_of(h)
            planes.add(p)
            total[p] += 1
            seen.add(p)
            if session_no(h) in gold_sessions:
                gold[p] += 1
                firsts.setdefault(p, rank)
        for p in seen:
            questions_with[p] += 1
        for p, r in firsts.items():
            first_rank_sum[p] += r
            first_rank_n[p] += 1
        sessions_all = {session_no(h) for h in top}
        hit_all += bool(sessions_all & gold_sessions)
        for p in planes:
            others = {session_no(h) for h in top if plane_of(h) != p}
            hit_without[p] += bool(others & gold_sessions)
    n = len(hits_per_question) or 1
    out: dict[str, Any] = {
        "questions": len(hits_per_question),
        "k": k,
        "gold_session_hit_at_k": hit_all,
        "planes": {},
    }
    for p in sorted(planes):
        out["planes"][p] = {
            "hits": total[p],
            "hits_per_question": round(total[p] / n, 2),
            "from_gold_session": gold[p],
            "precision": round(gold[p] / total[p], 3) if total[p] else 0.0,
            "questions_with_plane": questions_with[p],
            "first_gold_rank_mean": round(first_rank_sum[p] / first_rank_n[p], 2)
            if first_rank_n[p]
            else None,
            "gold_session_hit_at_k_without_plane": hit_without[p],
        }
    return out


async def run(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("LOOM_API_KEY", "")
    if not token:
        print("LOOM_API_KEY required", file=sys.stderr)
        return 2
    data = json.loads(Path(args.questions).read_text())
    sample = next(s for s in data if s.get("sample_id") == args.sample_id)
    qs = [q for q in sample["qa"] if evidence_sessions_cited(q)][: args.max_questions]
    pairs: list[tuple[list[dict[str, Any]], set[int]]] = []
    failures = 0
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=120.0,
    ) as c:
        for q in qs:
            body = {
                "org": args.org,
                "namespace": args.namespace,
                "agent": args.agent,
                "user_id": "-",
                "query": str(q.get("question") or ""),
                "top_k": args.top_k,
                "search_mode": args.search_mode,
                "namespace_scope": "this",
                "auto_touch": False,
            }
            if args.summary_cap is not None:
                body["summary_cap"] = int(args.summary_cap)  # request field
            resp: dict[str, Any] | None = None
            for attempt in range(5):
                try:
                    r = await c.post("/v1/memory.search", json=body)
                    if r.status_code >= 500 or r.status_code == 429:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    r.raise_for_status()
                    resp = r.json()
                    break
                except (RuntimeError, httpx.HTTPError):
                    await asyncio.sleep(2.0 * (attempt + 1))
            if resp is None:
                failures += 1
                continue
            pairs.append((list(resp.get("results") or []), evidence_sessions_cited(q)))
    out = tally(pairs, args.top_k)
    out.update(
        {
            "namespace": args.namespace,
            "search_mode": args.search_mode,
            "summary_cap": args.summary_cap,
            "failures": failures,
        }
    )
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(
        f"{'plane':<16} {'hits':>5} {'per q':>6} {'gold':>5} {'prec':>6} {'q with':>6} "
        f"{'1st gold':>8} {'hit@k w/o':>9}"
    )
    for p, s in out["planes"].items():
        print(
            f"{p:<16} {s['hits']:>5} {s['hits_per_question']:>6} {s['from_gold_session']:>5} "
            f"{s['precision']:>6} {s['questions_with_plane']:>6} "
            f"{str(s['first_gold_rank_mean']):>8} "
            f"{s['gold_session_hit_at_k_without_plane']:>9}"
        )
    print(
        f"gold-session hit@{args.top_k} with every plane: "
        f"{out['gold_session_hit_at_k']} of {out['questions']}"
    )
    print(f"wrote {args.out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=None)
    p.add_argument("--org", default="dev")
    p.add_argument("--namespace", required=True)
    p.add_argument("--agent", default="locomo-bench")
    p.add_argument("--questions", default="bench/datasets/locomo10.json")
    p.add_argument("--sample-id", default="conv-26")
    p.add_argument("--max-questions", type=int, default=500)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--search-mode", default="semantic")
    p.add_argument(
        "--summary-cap",
        type=int,
        default=None,
        help="per-request digest cap; omit for the deployment default",
    )
    p.add_argument("--out", default="bench/plane_precision.summary.json")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
