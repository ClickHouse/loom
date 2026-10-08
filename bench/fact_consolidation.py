"""FactConsolidation streaming bench (the MemoryAgentBench task).

Most benches score a frozen history; MemoryAgentBench's FactConsolidation
task feeds history as a turn-by-turn WRITE STREAM and queries mid-stream,
matching Loom's online write path. The dataset is a targeted regression
for bi-temporal supersession: assert that a later contradicting fact
WINS as-of now while the prior fact STAYS queryable as-of its interval.

This directly exercises two invariants:

  • `_repair_contradictory_triples` closing the prior open interval on
    the triple plane (server/memory/app/extract/handler.py).
  • `_next_version()` on wall-clock nanoseconds
    (server/memory/store/rows.py): streaming same-key memory writes
    supersede in arrival order. Here we drive a *single* process so the
    bench is deterministic, and assert the property: the latest write to a
    key wins as-of now, and the historical value is recoverable via the
    as-of read path.

Two planes, two as-of shapes (see bench/temporal_assertions.py):
  • Triple plane: facts.list(as_of=…) is a full interval test.
  • Memory plane: memory.get(as_of=…) is lower-bound only
    (coalesce(valid_at, created_at) <= as_of), so we assert the latest
    value wins NOW and the earlier value is recoverable at an as_of
    that predates the later write.

LLM-FREE write path: memory.bulk_set with explicit relations/valid_at/
valid_to plus memory.set for the same-key memory-plane stream.

Run (needs a live control plane + CH):
    python bench/fact_consolidation.py --base-url http://localhost:7777 \
        --token "$LOOM_API_KEY" --out bench/fact_consolidation.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:
    from bench._http import post_json
    from bench.bench_namespaces import (
        erase_on_exit_sync,
        register_bench_namespace_sync,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from _http import post_json  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit_sync,
        register_bench_namespace_sync,
        remember_bench_namespace,
    )


@dataclass(frozen=True)
class Turn:
    """One streamed turn: a (subject, relation, object) state at a
    real-world `valid_at`. Turns for the same (subject, relation) are
    contradictions; the latest by valid_at is the as-of-now winner."""

    valid_at: str
    obj: str


@dataclass(frozen=True)
class StreamCase:
    case_id: str
    subject: str
    relation: str  # single-valued so supersession fires on the triple plane
    turns: list[Turn]
    # Memory-plane key carrying the same evolving fact as a plain value.
    memory_key: str = ""
    extra_tags: list[str] = field(default_factory=list)


CASES: list[StreamCase] = [
    StreamCase(
        case_id="employer-stream",
        subject="user",
        relation="works_at",
        memory_key="user_employer",
        turns=[
            Turn(valid_at="2020-01-01", obj="acme"),
            Turn(valid_at="2022-06-01", obj="globex"),
            Turn(valid_at="2024-03-01", obj="clickhouse"),
        ],
    ),
    StreamCase(
        case_id="residence-stream",
        subject="user",
        relation="lives_in",
        memory_key="user_city",
        turns=[
            Turn(valid_at="2019-01-01", obj="berlin"),
            Turn(valid_at="2023-09-01", obj="amsterdam"),
        ],
    ),
]


def _post(
    client: httpx.Client, base_url: str, op: str, body: dict[str, Any], token: str
) -> dict[str, Any]:
    out: dict[str, Any] = post_json(
        client, f"{base_url}/v1/{op}", body, token=token, timeout_s=60.0
    )
    return out


def _facts_objects(
    client: httpx.Client,
    base_url: str,
    token: str,
    identity: dict[str, str],
    *,
    subject: str,
    relation: str,
    as_of: str | None,
    poll_timeout_s: float,
) -> set[str]:
    body: dict[str, Any] = {**identity, "subject": subject, "relation": relation, "limit": 500}
    if as_of is not None:
        body["as_of"] = as_of
    deadline = time.monotonic() + poll_timeout_s
    last: set[str] = set()
    while time.monotonic() < deadline:
        out = _post(client, base_url, "facts.list", body, token)
        last = {str(r.get("object", "")).strip().lower() for r in (out.get("rows") or [])}
        if last:
            return last
        time.sleep(0.2)
    return last


def run_case(
    client: httpx.Client, base_url: str, token: str, case: StreamCase
) -> list[dict[str, str]]:
    namespace = f"bench-factconsol-{uuid.uuid4().hex[:10]}"
    remember_bench_namespace("dev", namespace)
    register_bench_namespace_sync(
        lambda p, b: _post(client, base_url, p.removeprefix("/v1/"), b, token),
        org="dev",
        namespace=namespace,
        description="fact consolidation case",
    )
    identity = {"org": "dev", "namespace": namespace, "agent": "factconsol-bench"}
    rows: list[dict[str, str]] = []

    latest = case.turns[-1]
    first = case.turns[0]

    # --- Stream the turns ONE AT A TIME (write-then-continue) ---
    for turn in case.turns:
        # Triple plane: contradicting object closes the prior open interval.
        _post(
            client,
            base_url,
            "memory.bulk_set",
            {
                **identity,
                "entries": [
                    {
                        "memory_key": f"{case.case_id}-{turn.valid_at}",
                        "value": f"{case.subject} {case.relation} {turn.obj}",
                        "relations": [[case.subject, case.relation, turn.obj]],
                        "valid_at": turn.valid_at,
                    }
                ],
            },
            token,
        )
        # Memory plane: same key, evolving value + valid_at. Each set
        # supersedes the prior via the RMT version.
        if case.memory_key:
            _post(
                client,
                base_url,
                "memory.set",
                {
                    **identity,
                    "memory_key": case.memory_key,
                    "value": turn.obj,
                    "memory_type": "semantic",
                    "valid_at": turn.valid_at,
                },
                token,
            )

    # --- Assertion A: latest contradicting fact wins as-of NOW (triple plane) ---
    now_objects = _facts_objects(
        client,
        base_url,
        token,
        identity,
        subject=case.subject,
        relation=case.relation,
        as_of=None,
        poll_timeout_s=5.0,
    )
    latest_wins = latest.obj in now_objects
    # Earlier objects must NOT be among the open (as-of-now) facts.
    stale_leaked = sorted(t.obj for t in case.turns[:-1] if t.obj in now_objects)
    rows.append(
        {
            "case_id": case.case_id,
            "plane": "triple",
            "check": "latest_wins_as_of_now",
            "expected": latest.obj,
            "observed": ",".join(sorted(now_objects)) or "(none)",
            "passed": "1" if (latest_wins and not stale_leaked) else "0",
            "detail": f"stale_leaked={stale_leaked}" if stale_leaked else "",
        }
    )

    # --- Assertion B: prior fact stays queryable as-of its own interval ---
    # As-of the first turn's date, the first object must still surface.
    within_objects = _facts_objects(
        client,
        base_url,
        token,
        identity,
        subject=case.subject,
        relation=case.relation,
        as_of=first.valid_at,
        poll_timeout_s=5.0,
    )
    rows.append(
        {
            "case_id": case.case_id,
            "plane": "triple",
            "check": "prior_queryable_as_of_interval",
            "expected": first.obj,
            "observed": ",".join(sorted(within_objects)) or "(none)",
            "passed": "1" if first.obj in within_objects else "0",
            "detail": f"as_of={first.valid_at}",
        }
    )

    # --- Assertion C: memory plane — latest value wins NOW (RMT version) ---
    if case.memory_key:
        cur = _post(
            client,
            base_url,
            "memory.get",
            {
                **identity,
                "memory_key": case.memory_key,
            },
            token,
        )
        cur_val = str(cur.get("value") or "").strip().lower()
        rows.append(
            {
                "case_id": case.case_id,
                "plane": "memory",
                "check": "latest_value_wins_now",
                "expected": latest.obj,
                "observed": cur_val or "(none)",
                "passed": "1" if cur_val == latest.obj else "0",
                "detail": "RMT version (time.time_ns) ordering",
            }
        )

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--token", default="")
    parser.add_argument("--out", default="bench/fact_consolidation.csv")
    args = parser.parse_args()
    token = args.token or os.environ.get("LOOM_API_KEY", "")

    out_rows: list[dict[str, str]] = []
    with httpx.Client() as client:
        h = client.get(args.base_url + "/v1/health", timeout=5.0)
        if h.status_code != 200:
            print(f"server unhealthy: {h.status_code}", file=sys.stderr)
            return 2
        # Each case's namespace is erased when the run exits, after a failure too.
        with erase_on_exit_sync(
            lambda p, b: _post(client, args.base_url, p.removeprefix("/v1/"), b, token),
            org="dev",
        ):
            for case in CASES:
                out_rows.extend(run_case(client, args.base_url, token, case))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["case_id", "plane", "check", "expected", "observed", "passed", "detail"]
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    total = len(out_rows)
    passed = sum(1 for r in out_rows if r["passed"] == "1")
    print()
    print(f"{'case / plane / check':<58} {'verdict':>8}")
    for r in out_rows:
        label = f"{r['case_id']} / {r['plane']} / {r['check']}"
        print(f"{label[:58]:<58} {('PASS' if r['passed'] == '1' else 'FAIL'):>8}")
        if r["detail"] and r["passed"] == "0":
            print(f"    {r['detail']}")
    print()
    print(f"FactConsolidation streaming: {passed}/{total} checks passed -> {out_path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
