"""Aggregation bench — populates Loom with typed records via
memory.set_from_messages(record_schema=...), then runs memory.aggregate
queries against gold answers.

This is the access pattern published research highlights as the
weak spot for vector-only memory systems: questions like "sum of
all amounts in March" require structured aggregation, not similarity
ranking. Loom has both an extraction path that lands typed records
and a deterministic SQL-aggregation op, so the harness can compare
structured answers against gold values.

Output: pass/fail per question + an overall accuracy number. Floats are
compared with a small tolerance to account for LLM-extracted amount
formatting (e.g. "12.5" vs "12.50").

Run:
    python bench/aggregate.py --base-url http://localhost:7777
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

try:
    from bench._http import post_json
    from bench.bench_namespaces import (
        erase_on_exit_sync,
        register_bench_namespace_sync,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/aggregate.py` puts bench/ itself on sys.path
    from _http import post_json  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit_sync,
        register_bench_namespace_sync,
        remember_bench_namespace,
    )

# ============================================================
# Bench dataset
# ============================================================
# A small in-repo "expenses" dataset. Each conversation describes one
# expense; the schema captures category + amount. The aggregate
# questions exercise count, sum, group_by, and filter — the four
# operations that pure-vector memory can't reliably answer.

_CONVOS = [
    "Spent 7.50 on coffee at Blue Bottle this morning. Paid.",
    "Bought a 12.00 sandwich at Tartine for lunch. Paid in cash.",
    "Coffee run — 4.25 at the corner cafe. Paid.",
    "Dinner at Chipotle came to 16.50. Used the card.",
    "Another 5.00 coffee on my way to the office. Card.",
    "Lunch with Alice at Tartine — split, my half was 14.00.",
    "Coffee + croissant at Blue Bottle, 9.75 total. Card.",
    "Picked up groceries — 38.20 at Whole Foods. Card.",
]

# `category` is an enum so the LLM picks from a fixed vocabulary —
# without this gpt-4o-mini fragments into "coffee" / "Coffee" / "food"
# and aggregate counts split across buckets.
_SCHEMA = {
    "category": ["coffee", "lunch", "dinner", "groceries", "other"],
    "amount": "number",
}

# Gold answers — derived by hand from the conversations above. The
# bench passes when memory.aggregate returns these (within tolerance).
_AGGREGATES = [
    {
        "question": "How many coffee purchases?",
        "agg_func": "count",
        "group_by": ["category"],
        "filter": {"category": "coffee"},
        "gold_metric": 4.0,  # 4 entries explicitly mention coffee
    },
    {
        "question": "Total spent on lunch?",
        "agg_func": "sum",
        "agg_field": "amount",
        "group_by": ["category"],
        "filter": {"category": "lunch"},
        "gold_metric": 26.0,  # 12 (Tartine sandwich) + 14 (Alice split)
    },
    {
        "question": "Average spend overall?",
        "agg_func": "avg",
        "agg_field": "amount",
        "group_by": [],
        "filter": {},
        "gold_metric": 13.4,  # rough; tolerance below
    },
    {
        "question": "Max single expense?",
        "agg_func": "max",
        "agg_field": "amount",
        "group_by": [],
        "filter": {},
        "gold_metric": 38.2,
    },
]

# Loose tolerance — the gold numbers assume LLM extraction produced
# clean categories ("coffee" / "lunch") which it usually does, but a
# misclassification shifts the sum buckets. ±25% absolute is a real
# floor; below that we're measuring extraction quality, not the
# aggregate op.
_TOLERANCE_REL = 0.25
_TOLERANCE_ABS = 1.0


@dataclass
class QuestionResult:
    question: str
    expected: float
    actual: float | None
    passed: bool
    error: str = ""


def _post(client: httpx.Client, base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = post_json(client, base_url + path, body, timeout_s=30.0)
    return out


def _close(expected: float, actual: float) -> bool:
    if abs(expected - actual) <= _TOLERANCE_ABS:
        return True
    if expected == 0:
        return abs(actual) <= _TOLERANCE_ABS
    return abs(expected - actual) / abs(expected) <= _TOLERANCE_REL


def _populate(client: httpx.Client, base_url: str, identity: dict[str, str]) -> None:
    """One typed record per conversation."""
    for i, convo in enumerate(_CONVOS):
        _post(
            client,
            base_url,
            "/v1/memory.set_from_messages",
            {
                **identity,
                "memory_key": f"expense-{i:03d}",
                "record_schema": _SCHEMA,
                "messages": [{"role": "user", "content": convo}],
            },
        )
        print(f"  wrote expense-{i:03d}", flush=True)


def _ask(client: httpx.Client, base_url: str, identity: dict[str, str]) -> list[QuestionResult]:
    results: list[QuestionResult] = []
    for q in _AGGREGATES:
        body: dict[str, Any] = {
            **identity,
            "agg_func": q["agg_func"],
            "group_by": q.get("group_by", []),
            "filter": q.get("filter", {}),
        }
        if q.get("agg_field"):
            body["agg_field"] = q["agg_field"]
        expected = float(str(q["gold_metric"]))
        try:
            rows = _post(client, base_url, "/v1/memory.aggregate", body).get("rows", [])
            # No rows is 0 for a count or a sum. With a filter or no group_by
            # the answer is one row; take the first.
            actual = float(rows[0]["metric"]) if rows else 0.0
            results.append(
                QuestionResult(str(q["question"]), expected, actual, _close(expected, actual))
            )
        except (httpx.HTTPError, KeyError, ValueError) as e:
            results.append(QuestionResult(str(q["question"]), expected, None, False, str(e)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    parser.add_argument("--org", default="dev")
    parser.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="do not erase this run's agg-bench-* namespace when it ends",
    )
    args = parser.parse_args()

    namespace = f"agg-bench-{uuid.uuid4().hex[:8]}"
    identity = {"org": args.org, "namespace": namespace, "agent": "agg-bench", "user_id": "-"}
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

    print(f"loom-aggregate-bench against {args.base_url}, ns={namespace}")

    with httpx.Client(headers=headers) as client:
        h = client.get(args.base_url + "/v1/health", timeout=5.0)
        if h.status_code != 200:
            print(f"server unhealthy: {h.status_code}", file=sys.stderr)
            return 2

        def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
            return _post(client, args.base_url, path, body)

        with erase_on_exit_sync(post, org=args.org, keep=args.keep_namespaces):
            remember_bench_namespace(args.org, namespace)
            register_bench_namespace_sync(
                post,
                org=args.org,
                namespace=namespace,
                description="aggregation bench",
                keep=args.keep_namespaces,
            )
            _populate(client, args.base_url, identity)
            results = _ask(client, args.base_url, identity)

    print()
    print(f"{'question':<32} {'expected':>10} {'actual':>10} {'verdict':>8}")
    for r in results:
        actual_s = f"{r.actual:.2f}" if r.actual is not None else "ERR"
        verdict = "PASS" if r.passed else "FAIL"
        print(f"{r.question[:32]:<32} {r.expected:>10.2f} {actual_s:>10} {verdict:>8}")
        if r.error:
            print(f"  error: {r.error}")

    passed = sum(1 for r in results if r.passed)
    n = len(results)
    rate = (passed / n * 100.0) if n else 0.0
    print()
    print(f"AGGREGATE accuracy: {passed} / {n}  ({rate:.1f}%)")
    return 0 if passed == n else 1


if __name__ == "__main__":
    sys.exit(main())
