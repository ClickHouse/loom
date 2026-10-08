"""Extraction regression harness for memory.set_from_messages.

Dataset format:
[
  {
    "id": "case-1",
    "messages": [{"role": "user", "content": "..."}],
    "expected": ["deploy window", "user prefers python"]
  }
]

The harness extracts into a fresh namespace, lists memories, and scores
whether each expected phrase appears in any memory key or value excerpt.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
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
from bench.failure_taxonomy import classify_extraction_failure

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", text.lower()).strip()


def _post(
    client: httpx.Client,
    base_url: str,
    path: str,
    body: dict[str, Any],
    token: str,
) -> dict[str, Any]:
    out: dict[str, Any] = post_json(client, base_url + path, body, token=token, timeout_s=60.0)
    return out


def _memory_text(entry: dict[str, Any]) -> str:
    return _norm(
        str(entry.get("memory_key", ""))
        + " "
        + str(entry.get("value", ""))
        + " "
        + str(entry.get("content_excerpt", ""))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    parser.add_argument("--out", default="bench/extraction_eval.csv")
    args = parser.parse_args()

    cases = json.loads(Path(args.dataset).read_text())
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []

    # Each case's namespace is erased when the run exits, after a failure too.
    with (
        httpx.Client() as client,
        erase_on_exit_sync(lambda p, b: _post(client, args.base_url, p, b, args.token), org="dev"),
    ):
        for idx, case in enumerate(cases):
            namespace = f"bench-extract-eval-{uuid.uuid4().hex[:10]}"
            remember_bench_namespace("dev", namespace)
            register_bench_namespace_sync(
                lambda p, b: _post(client, args.base_url, p, b, args.token),
                org="dev",
                namespace=namespace,
                description="extraction regression case",
            )
            identity = {"org": "dev", "namespace": namespace, "agent": "extract-eval"}
            _post(
                client,
                args.base_url,
                "/v1/memory.set_from_messages",
                {**identity, "messages": case.get("messages") or case.get("conversation") or []},
                args.token,
            )
            listed = _post(
                client,
                args.base_url,
                "/v1/memory.list",
                {**identity, "limit": 500},
                args.token,
            )
            entries = list(listed.get("entries") or [])
            corpus = " ".join(_memory_text(e) for e in entries)
            for expected in case.get("expected", []):
                hit = _norm(str(expected)) in corpus
                rows.append(
                    {
                        "case_id": str(case.get("id", idx)),
                        "expected": str(expected),
                        "matched": "1" if hit else "0",
                        "failure_stage": classify_extraction_failure(
                            matched=hit,
                            memory_count=len(entries),
                        ),
                        "memory_keys": " | ".join(str(e.get("memory_key", "")) for e in entries),
                    }
                )

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["case_id", "expected", "matched", "failure_stage", "memory_keys"],
        )
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    matched = sum(1 for r in rows if r["matched"] == "1")
    rate = matched / total * 100 if total else 0.0
    print(f"extraction precision proxy={rate:.1f}% ({matched}/{total}) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
