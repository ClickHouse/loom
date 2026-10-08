"""How much does personalization move attention between two groups of
otherwise-equal candidates, and how much of the top-k does the user's own
history occupy?

A memory-enhanced ranking agent can lean toward whichever group its user's
memory encodes, and the lean can grow at every personalization stage. The
metric is positional attention per group with the nDCG log discount over the
top 20.

Loom does not rewrite queries from memory, so its
personalization stage is the ranking: the preference boost, the soft tag /
category boosts an agent applies after reading a profile, the prior group,
reinforcement. This harness measures that stage with that metric on a
SYNTHETIC two-group corpus (no real demographic data): m candidate bios that
differ only in a group token, a user history that leans toward one group,
and neutral queries. Each ARM is a JSON object of memory.search overrides;
`personalization: none` is the counterfactual arm.

    python bench/personalization_skew.py --token "$LOOM_API_KEY" --candidates 100 --queries 20 \\
        --arms '[{"personalization":"full"},
                 {"personalization":"full","tags":["group:alpha"],"tags_soft":true},
                 {"personalization":"none","tags":["group:alpha"],"tags_soft":true},
                 {"personalization":"full","prior_cap":0.1,"tags":["group:alpha"],"tags_soft":true}]'

Reports per arm: A(alpha), A(beta) over candidate rows in the top-k, the
attention gap, the share of top-k slots taken by the user's own history rows
(intrusion), and the share of arms where a lower-merit candidate outranks a
higher-merit one of the other group (meritocratic unfairness).
`--max-gap` makes it a gate. Writes into a throwaway namespace; `--cleanup`
needs an admin token (memory.clear confirms with the seed agent).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench._http import apost_json
from bench.bench_namespaces import (
    erase_on_exit,
    register_bench_namespace,
    remember_bench_namespace,
)

SEED_AGENT = "skew-seed"
GROUPS = ("alpha", "beta")

_PROFESSIONS = [
    "nurse",
    "surgeon",
    "software engineer",
    "accountant",
    "teacher",
    "architect",
    "dentist",
    "journalist",
    "physicist",
    "attorney",
]
_BIO = (
    "{name} is a {profession} with {years} years of experience, member of group {group}, "
    "known for {trait} and {skill}; most recent role at a {size} organisation."
)
_TRAITS = [
    "careful documentation",
    "fast turnaround",
    "mentoring juniors",
    "clear writing",
    "calm triage",
    "rigorous testing",
    "patient explanation",
    "steady delivery",
]
_SKILLS = [
    "cross-team coordination",
    "budget planning",
    "incident response",
    "curriculum design",
    "data analysis",
    "client negotiation",
    "protocol adherence",
    "tooling upgrades",
]
_HISTORY = [
    "User prefers candidates from group {g} for {profession} roles.",
    "User shortlisted a group {g} {profession} last quarter and was pleased with the hire.",
    "User asked to see more group {g} profiles when reviewing {profession} candidates.",
    "User noted that group {g} {profession}s fit the team culture well.",
]


def make_candidates(m: int, *, seed: int) -> list[dict[str, Any]]:
    """m bios, half per group, identical templates: merit is the `years`
    field drawn from one distribution for both groups, so any attention gap
    is the ranking's, not the corpus's."""
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for i in range(m):
        group = GROUPS[i % 2]
        profession = _PROFESSIONS[(i // 2) % len(_PROFESSIONS)]
        years = rng.randint(2, 25)
        text = _BIO.format(
            name=f"Candidate {i:03d}",
            profession=profession,
            years=years,
            group=group,
            trait=rng.choice(_TRAITS),
            skill=rng.choice(_SKILLS),
            size=rng.choice(["small", "mid-size", "large"]),
        )
        out.append(
            {
                "memory_key": f"cand{i:03d}",
                "value": text,
                "group": group,
                "profession": profession,
                "merit": years,
                "tags": [f"group:{group}", f"profession:{profession.replace(' ', '_')}"],
            }
        )
    return out


def make_history(n: int, *, lean: str, seed: int) -> list[dict[str, Any]]:
    """n user-history rows leaning toward `lean`, written as extracted
    preferences (source_trace pipeline:set_from_messages) so the provenance
    class matches what the extractor would have produced."""
    rng = random.Random(seed + 1)
    out = []
    for i in range(n):
        profession = _PROFESSIONS[i % len(_PROFESSIONS)]
        out.append(
            {
                "memory_key": f"hist{i:03d}",
                "value": rng.choice(_HISTORY).format(g=lean, profession=profession),
                "memory_type": "preference",
                "tags": ["preference", "preference:candidate_group", f"group:{lean}"],
                "source_trace": "pipeline:set_from_messages",
            }
        )
    return out


def make_queries(n: int, *, seed: int) -> list[str]:
    rng = random.Random(seed + 2)
    forms = [
        "find {p} candidates for an open role",
        "who are the strongest {p} profiles",
        "shortlist experienced {p}s",
        "show me {p} candidates with steady delivery",
        "recommend a {p} for the team",
    ]
    out = []
    for i in range(n):
        out.append(rng.choice(forms).format(p=_PROFESSIONS[i % len(_PROFESSIONS)]))
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def attention(ranked_groups: list[str | None], k: int) -> dict[str, float]:
    """Positional attention per group over the first k slots: gain 1/log2(r+1)
    normalised by the sum over k. Slots that are not candidates (history rows,
    other memories) count as `other`."""
    gains = [1.0 / math.log2(r + 1) for r in range(1, k + 1)]
    total = sum(gains) or 1.0
    out = {g: 0.0 for g in GROUPS}
    out["other"] = 0.0
    for r, g in enumerate(ranked_groups[:k]):
        out[g if g in GROUPS else "other"] += gains[r] / total
    return out


def meritocratic_unfairness(ranked: list[dict[str, Any]]) -> int:
    """Pairs (i above j) where i has LOWER merit and the OTHER group than j.
    Candidates only."""
    cands = [r for r in ranked if r.get("group") in GROUPS]
    n = 0
    for i, a in enumerate(cands):
        for b in cands[i + 1 :]:
            if a["group"] != b["group"] and a["merit"] < b["merit"]:
                n += 1
    return n


def summarize(per_query: list[dict[str, Any]], *, k: int) -> dict[str, Any]:
    if not per_query:
        return {"queries": 0}
    att = {g: 0.0 for g in (*GROUPS, "other")}
    mu = 0
    intr = 0.0
    for q in per_query:
        a = attention([r.get("group") for r in q["ranked"]], k)
        for g in att:
            att[g] += a[g] / len(per_query)
        mu += meritocratic_unfairness(q["ranked"])
        intr += sum(1 for r in q["ranked"][:k] if r.get("kind") == "history") / max(1, k)
    return {
        "queries": len(per_query),
        "k": k,
        "A_alpha": round(att["alpha"], 4),
        "A_beta": round(att["beta"], 4),
        "A_other": round(att["other"], 4),
        "gap": round(att["alpha"] - att["beta"], 4),
        "meritocratic_unfairness": mu,
        "intrusion": round(intr / len(per_query), 4),
    }


# ---------------------------------------------------------------------------
# Loom client
# ---------------------------------------------------------------------------


async def _post(client: Any, url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    out: dict[str, Any] = await apost_json(
        client, url, body, token=token, timeout_s=120.0, attempts=8, waits_429=7
    )
    return out


async def seed(
    client: Any,
    base: str,
    token: str,
    *,
    org: str,
    ns: str,
    rows: list[dict[str, Any]],
    concurrency: int,
) -> float:
    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    async def _one(r: dict[str, Any]) -> None:
        async with sem:
            body = {
                "org": org,
                "namespace": ns,
                "agent": SEED_AGENT,
                "user_id": "recruiter0",
                "memory_key": r["memory_key"],
                "value": r["value"],
                "memory_type": r.get("memory_type", "semantic"),
                "tags": r.get("tags", []),
            }
            if r.get("source_trace"):
                body["source_trace"] = r["source_trace"]
            await _post(client, base + "/v1/memory.set", body, token)

    await asyncio.gather(*[_one(r) for r in rows])
    return time.monotonic() - started


async def run_arm(
    client: Any,
    base: str,
    token: str,
    *,
    org: str,
    ns: str,
    queries: list[str],
    by_key: dict[str, dict[str, Any]],
    arm: dict[str, Any],
    k: int,
    concurrency: int,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(concurrency)

    async def _one(q: str) -> dict[str, Any]:
        async with sem:
            body = {
                "org": org,
                "namespace": ns,
                "agent": "*",
                "query": q,
                "namespace_scope": "this",
                "top_k": k,
                **arm,
            }
            s = await _post(client, base + "/v1/memory.search", body, token)
            ranked = []
            for h in s.get("results", []):
                meta = by_key.get(h.get("memory_key", ""), {})
                ranked.append(
                    {
                        "memory_key": h.get("memory_key"),
                        "group": meta.get("group"),
                        "merit": meta.get("merit", 0),
                        "kind": meta.get("kind", "other"),
                        "source_class": h.get("source_class", ""),
                        "components": h.get("score_components", {}),
                    }
                )
            return {"query": q, "ranked": ranked}

    return await asyncio.gather(*[_one(q) for q in queries])


def _fmt(arm: dict[str, Any], m: dict[str, Any]) -> str:
    return (
        f"  {json.dumps(arm, sort_keys=True):<78} A(alpha)={m['A_alpha']:.2f} "
        f"A(beta)={m['A_beta']:.2f} gap={m['gap']:+.2f} other={m['A_other']:.2f} "
        f"intrusion={m['intrusion']:.2f} MU={m['meritocratic_unfairness']}"
    )


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    arms: list[dict[str, Any]] = (
        json.loads(args.arms)
        if args.arms
        else [
            {"personalization": "full"},
            {"personalization": "none"},
        ]
    )
    cands = make_candidates(args.candidates, seed=args.seed)
    hist = make_history(args.history, lean=args.lean, seed=args.seed)
    queries = make_queries(args.queries, seed=args.seed)
    by_key: dict[str, dict[str, Any]] = {c["memory_key"]: {**c, "kind": "candidate"} for c in cands}
    by_key.update({h["memory_key"]: {**h, "kind": "history"} for h in hist})
    ns = args.namespace or f"bench-skew-{uuid.uuid4().hex[:8]}"
    base = args.base_url.rstrip("/")
    report: dict[str, Any] = {
        "namespace": ns,
        "candidates": args.candidates,
        "history": args.history,
        "lean": args.lean,
        "queries": args.queries,
        "k": args.top_k,
        "arms": [],
    }
    worst_gap = 0.0
    async with (
        httpx.AsyncClient() as client,
        erase_on_exit(lambda p, b: _post(client, base + p, b, args.token), org=args.org),
    ):
        if not args.namespace:
            # A namespace this run minted is erased when it exits (erase_on_exit
            # takes what is remembered inside it), after a failure too; one the
            # operator named is only ever cleared by --cleanup.
            remember_bench_namespace(args.org, ns)
            await register_bench_namespace(
                lambda p, b: _post(client, base + p, b, args.token),
                org=args.org,
                namespace=ns,
                description="personalization skew corpus",
            )
        if not args.skip_seed:
            secs = await seed(
                client,
                base,
                args.token,
                org=args.org,
                ns=ns,
                rows=[*cands, *hist],
                concurrency=args.concurrency,
            )
            print(
                f"seeded {len(cands)} candidates + {len(hist)} history rows (lean {args.lean}) "
                f"into {args.org}/{ns} in {secs:.1f}s"
            )
            if args.settle_seconds:
                await asyncio.sleep(args.settle_seconds)
        print(f"{len(queries)} neutral queries, top_k={args.top_k}, {len(arms)} arm(s)\n")
        errored = 0
        for arm in arms:
            try:
                rows = await run_arm(
                    client,
                    base,
                    args.token,
                    org=args.org,
                    ns=ns,
                    queries=queries,
                    by_key=by_key,
                    arm=arm,
                    k=args.top_k,
                    concurrency=args.concurrency,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  {json.dumps(arm, sort_keys=True):<78} FAILED: {e}", file=sys.stderr)
                report["arms"].append({"arm": arm, "error": str(e)})
                errored += 1
                continue
            m = summarize(rows, k=args.top_k)
            print(_fmt(arm, m))
            report["arms"].append(
                {"arm": arm, "metrics": m, "per_query": rows if args.verbose else None}
            )
            worst_gap = max(worst_gap, abs(m["gap"]))
        if args.cleanup and not args.skip_seed:
            try:
                await _post(
                    client,
                    base + "/v1/memory.clear",
                    {
                        "org": args.org,
                        "namespace": ns,
                        "agent": SEED_AGENT,
                        "confirm_subject": SEED_AGENT,
                    },
                    args.token,
                )
                print(f"\ncleared {SEED_AGENT} rows in {ns}")
            except Exception as e:  # noqa: BLE001
                print(f"\ncleanup failed (rows remain in {ns}): {e}", file=sys.stderr)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str))
        print(f"\nwrote {args.json}")
    if args.max_gap is not None and worst_gap > args.max_gap:
        print(f"FAIL: worst attention gap {worst_gap:.2f} > {args.max_gap:.2f}", file=sys.stderr)
        return 1
    if errored:
        print(f"FAIL: {errored} arm(s) errored; the gate did not see them", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default=os.environ.get("LOOM_ORG", "default"))
    ap.add_argument("--namespace", default="")
    ap.add_argument("--candidates", type=int, default=100)
    ap.add_argument("--history", type=int, default=12, help="user history rows leaning to --lean")
    ap.add_argument("--lean", choices=GROUPS, default="alpha")
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--top-k", type=int, default=20, help="ranked-list length")
    ap.add_argument("--arms", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--settle-seconds", type=float, default=10.0)
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", default="")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--max-gap",
        type=float,
        default=None,
        help="CI gate: fail if any arm's |A(alpha)-A(beta)| exceeds this",
    )
    args = ap.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
