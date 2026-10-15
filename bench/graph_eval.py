"""Graph-building regression harness for extracted facts/triples.

Dataset format:
[
  {
    "id": "employment-window",
    "messages": [{"role": "user", "content": "I worked at Acme until 2024."}],
    "expected_triples": [
      {"subject": "user", "relation": "worked_at", "object": "acme"}
    ],
    "as_of": "2023-01-01"
  }
]

The harness writes each case via memory.set_from_messages, then queries
facts.list and checks whether the expected triples are present. This is
separate from answer-quality benches: it measures graph extraction stability.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from bench._http import post_json
from bench.bench_namespaces import (
    erase_on_exit_sync,
    register_bench_namespace_sync,
    remember_bench_namespace,
)
from bench.failure_taxonomy import classify_graph_failure


def _post(
    client: httpx.Client,
    base_url: str,
    op: str,
    body: dict[str, Any],
    token: str,
) -> dict[str, Any]:
    out: dict[str, Any] = post_json(
        client, f"{base_url}/v1/{op}", body, token=token, timeout_s=60.0
    )
    return out


def _canon_triple(raw: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(raw.get("subject", "")).strip().lower(),
        str(raw.get("relation", "")).strip().lower(),
        str(raw.get("object", "")).strip().lower(),
    )


def _facts_for_case(
    client: httpx.Client,
    base_url: str,
    token: str,
    identity: dict[str, str],
    *,
    as_of: str | None,
    timeout_s: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {**identity, "limit": 500}
    if as_of:
        body["valid_as_of"] = as_of
    while time.monotonic() < deadline:
        out = _post(client, base_url, "facts.list", body, token)
        rows = list(out.get("rows") or [])
        if rows:
            return rows
        time.sleep(0.2)
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    parser.add_argument("--out", default="bench/graph_eval.csv")
    parser.add_argument("--poll-timeout-s", type=float, default=5.0)
    args = parser.parse_args()

    cases = json.loads(Path(args.dataset).read_text())
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result_rows: list[dict[str, Any]] = []

    # Each case's namespace is erased when the run exits, after a failure too.
    with (
        httpx.Client() as client,
        erase_on_exit_sync(
            lambda p, b: _post(client, args.base_url, p.removeprefix("/v1/"), b, args.token),
            org="dev",
        ),
    ):
        for idx, case in enumerate(cases):
            namespace = f"bench-graph-eval-{uuid.uuid4().hex[:10]}"
            remember_bench_namespace("dev", namespace)
            register_bench_namespace_sync(
                lambda p, b: _post(client, args.base_url, p.removeprefix("/v1/"), b, args.token),
                org="dev",
                namespace=namespace,
                description="graph extraction regression case",
            )
            identity = {"org": "dev", "namespace": namespace, "agent": "graph-eval"}
            _post(
                client,
                args.base_url,
                "memory.set_from_messages",
                {**identity, "messages": case.get("messages") or []},
                args.token,
            )
            rows = _facts_for_case(
                client,
                args.base_url,
                args.token,
                identity,
                as_of=case.get("as_of"),
                timeout_s=args.poll_timeout_s,
            )
            observed = {_canon_triple(r) for r in rows}
            for expected in case.get("expected_triples", []):
                triple = _canon_triple(expected)
                result_rows.append(
                    {
                        "case_id": str(case.get("id", idx)),
                        "expected": " ".join(triple),
                        "matched": "1" if triple in observed else "0",
                        "failure_stage": classify_graph_failure(
                            matched=triple in observed,
                            observed_count=len(observed),
                        ),
                        "as_of": str(case.get("as_of") or ""),
                        "observed": " | ".join(" ".join(t) for t in sorted(observed)),
                    }
                )

    with out_path.open("w", newline="") as f:
        fieldnames = ["case_id", "expected", "matched", "failure_stage", "as_of", "observed"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(result_rows)

    total = len(result_rows)
    matched = sum(1 for r in result_rows if r["matched"] == "1")
    rate = matched / total * 100 if total else 0.0
    print(f"graph extraction recall={rate:.1f}% ({matched}/{total}) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
