"""Retention over periods: do seeded facts still come back after the store
has moved on N times?

"Retention after six periods" is a stability number worth having for a
memory store: a fact learned in period 0 must still be retrievable at period 6 once
six rounds of unrelated writes have landed around it. LoCoMo accuracy falls as evidence spans more
sessions, which shows the shape only indirectly, and the forgetting bench
checks the bi-temporal invariant, not decay under noise.

This bench is LLM-free and deterministic on the write side. It seeds
`--facts` distinctive facts into a fresh namespace, then for each of
`--periods` rounds ingests `--noise` unrelated facts (a different topic per
period) and re-searches every seeded fact with its question, recording per
period: recall@5, recall@10, mean rank of the gold, and how many seeded facts
fell out of the top-10 entirely. With `--ask` it also asks each question and
scores whether the answer cites the seeded key (the ±1 cite score of
bench/hard_distractors.py), which is the accuracy side of the curve.

Run:
    LOOM_API_KEY=... python bench/retention_curve.py --base-url URL \\
        --org <org> --facts 20 --periods 6 --noise 40 --out /tmp/retention.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_TOPICS = [
    "deploy windows",
    "on-call rotations",
    "vector index settings",
    "release dates",
    "office moves",
    "team sizes",
    "cost caps",
    "review SLAs",
    "cache lifetimes",
    "bench scores",
]
_NOISE_TOPICS = [
    "recipes",
    "hiking routes",
    "board games",
    "car maintenance",
    "gardening",
    "music theory",
    "chess openings",
    "astronomy",
    "knitting patterns",
    "language learning",
]


def make_facts(n: int, seed: int = 0) -> list[dict[str, str]]:
    """Distinctive seed facts with a question each; deterministic for (n, seed)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        topic = _TOPICS[i % len(_TOPICS)]
        who = rng.choice(["Marta", "Kenji", "Lena", "Tomas", "Priya", "Ines", "Rafael", "Yuki"])
        num = rng.randint(3, 97)
        day = rng.randint(1, 28)
        text = (
            f"{who} recorded that the {topic} value number {i} is {num}, set on 2026-03-{day:02d}."
        )
        out.append(
            {
                "key": f"seed_{seed}_{i:03d}",
                "value": text,
                "question": f"What is the {topic} value number {i}, and who recorded it?",
            }
        )
    return out


def make_noise(m: int, period: int, seed: int = 0) -> list[dict[str, str]]:
    """Unrelated facts for one period; a different topic per period so the
    noise is not accidentally near the seeds."""
    rng = random.Random(f"{seed}:{period}")
    topic = _NOISE_TOPICS[period % len(_NOISE_TOPICS)]
    return [
        {
            "key": f"noise_{seed}_{period:02d}_{j:03d}",
            "value": (
                f"Note {j} on {topic}: item {rng.randint(100, 999)} was reviewed on "
                f"2026-0{rng.randint(1, 9)}-{rng.randint(10, 28)} "
                f"and rated {rng.randint(1, 5)} of 5."
            ),
        }
        for j in range(m)
    ]


def gold_rank(returned: list[str], key: str) -> int | None:
    for i, k in enumerate(returned, start=1):
        if k == key:
            return i
    return None


@dataclass
class PeriodResult:
    period: int
    rows_in_namespace: int
    recall_at_5: float
    recall_at_10: float
    mean_rank: float | None
    lost: int  # seeded facts absent from the top-10
    cite_score_mean: float | None = None
    gold_cited_share: float | None = None
    ranks: dict[str, int | None] = field(default_factory=dict)


def score_period(
    period: int,
    rows: int,
    ranks: dict[str, int | None],
    cites: list[tuple[float, bool]] | None = None,
) -> PeriodResult:
    found = [r for r in ranks.values() if r is not None]
    n = len(ranks) or 1
    cs = [c for c, _ in (cites or [])]
    return PeriodResult(
        period=period,
        rows_in_namespace=rows,
        recall_at_5=round(sum(1 for r in found if r <= 5) / n, 3),
        recall_at_10=round(sum(1 for r in found if r <= 10) / n, 3),
        mean_rank=round(sum(found) / len(found), 2) if found else None,
        lost=sum(1 for r in ranks.values() if r is None or r > 10),
        cite_score_mean=round(sum(cs) / len(cs), 3) if cs else None,
        gold_cited_share=round(sum(1 for _, g in (cites or []) if g) / len(cites), 3)
        if cites
        else None,
        ranks=dict(ranks),
    )


def render(periods: list[PeriodResult]) -> str:
    lines = [
        "| period | rows | recall@5 | recall@10 | mean rank | lost | cite score | gold cited |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in periods:
        cs = "—" if p.cite_score_mean is None else f"{p.cite_score_mean:+.2f}"
        gc = "—" if p.gold_cited_share is None else f"{p.gold_cited_share:.2f}"
        lines.append(
            f"| {p.period} | {p.rows_in_namespace} | {p.recall_at_5:.3f} | {p.recall_at_10:.3f} | "
            f"{p.mean_rank if p.mean_rank is not None else '—'} | {p.lost} | {cs} | {gc} |"
        )
    if len(periods) >= 2:
        d = periods[-1].recall_at_10 - periods[0].recall_at_10
        lines.append("")
        lines.append(
            f"retention after {periods[-1].period} periods: "
            f"recall@10 {periods[0].recall_at_10:.3f} → {periods[-1].recall_at_10:.3f} "
            f"({d:+.3f}); lost {periods[-1].lost} of {len(periods[-1].ranks)}"
        )
    return "\n".join(lines)


async def run(a: argparse.Namespace) -> list[PeriodResult]:
    import httpx

    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

    token = a.token or os.environ.get("LOOM_API_KEY", "")
    if not token:
        raise SystemExit("--token or LOOM_API_KEY required")
    ns = a.namespace or f"bench-retention-{int(time.time())}"
    ident = {"org": a.org, "namespace": ns, "agent": "retention-bench"}
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = a.base_url.rstrip("/")
    facts = make_facts(a.facts, a.seed)
    rows = 0
    out: list[PeriodResult] = []
    async with httpx.AsyncClient(timeout=120.0) as c:

        async def op(name: str, body: dict[str, Any]) -> dict[str, Any]:
            r = await c.post(f"{base}/v1/{name}", json=body, headers=H)
            r.raise_for_status()
            return r.json()

        async def write(entries: list[dict[str, str]]) -> int:
            n = 0
            for i in range(0, len(entries), 25):
                batch = entries[i : i + 25]
                await op(
                    "memory.bulk_set",
                    {
                        **ident,
                        "session_id": f"s{int(time.time() * 1000)}",
                        "entries": [
                            {"memory_key": e["key"], "memory_type": "semantic", "value": e["value"]}
                            for e in batch
                        ],
                    },
                )
                n += len(batch)
            return n

        async def measure(period: int) -> PeriodResult:
            ranks: dict[str, int | None] = {}
            cites: list[tuple[float, bool]] = []
            for f in facts:
                r = await op(
                    "memory.search",
                    {
                        **ident,
                        "agent": "*",
                        "query": f["question"],
                        "top_k": 10,
                        "namespace_scope": "this",
                    },
                )
                ranks[f["key"]] = gold_rank(
                    [h.get("memory_key", "") for h in r.get("results", [])], f["key"]
                )
                if a.ask:
                    ans = await op(
                        "memory.ask",
                        {
                            **ident,
                            "agent": "*",
                            "query": f["question"],
                            "top_k": 10,
                            "namespace_scope": "this",
                            "precedents": False,
                        },
                    )
                    from bench.hard_distractors import cite_score, cited_keys

                    cited = cited_keys(str(ans.get("answer") or ""), list(ans.get("results") or []))
                    cites.append(cite_score(cited, [f["key"]]))
            return score_period(period, rows, ranks, cites if a.ask else None)

        def post(path: str, body: dict[str, Any]) -> Any:
            return op(path.removeprefix("/v1/"), body)

        # A minted namespace is erased when the run exits, after a failure or
        # Ctrl-C too; one the operator named with --namespace is never erased.
        async with erase_on_exit(post, org=a.org, keep=a.keep_namespaces):
            if not a.namespace:
                remember_bench_namespace(a.org, ns)
                await register_bench_namespace(
                    post,
                    org=a.org,
                    namespace=ns,
                    description="retention curve",
                    keep=a.keep_namespaces,
                )
            rows += await write(facts)
            await asyncio.sleep(a.settle_seconds)
            out.append(await measure(0))
            for period in range(1, a.periods + 1):
                rows += await write(make_noise(a.noise, period, a.seed))
                await asyncio.sleep(a.settle_seconds)
                out.append(await measure(period))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default="")
    p.add_argument("--org", required=True)
    p.add_argument(
        "--namespace",
        default="",
        help="default: a fresh bench-retention-<ts> namespace, erased when the run ends",
    )
    p.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="do not erase the minted bench-retention-* namespace at the end",
    )
    p.add_argument("--facts", type=int, default=20)
    p.add_argument("--periods", type=int, default=6)
    p.add_argument("--noise", type=int, default=40, help="unrelated facts ingested per period")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--settle-seconds", type=float, default=5.0)
    p.add_argument("--ask", action="store_true", help="also score memory.ask citations per period")
    p.add_argument("--out", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--min-recall",
        type=float,
        default=None,
        help="exit 1 when the last period's recall@10 is below this",
    )
    a = p.parse_args(argv)
    periods = asyncio.run(run(a))
    if a.out:
        Path(a.out).write_text(json.dumps([asdict(x) for x in periods], indent=1))
    print(json.dumps([asdict(x) for x in periods], indent=2) if a.json else render(periods))
    if a.min_recall is not None and periods and periods[-1].recall_at_10 < a.min_recall:
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
