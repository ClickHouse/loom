"""Forgetting-aware regression for Loom's bi-temporal invariant.

A forgetting-aware metric penalizes responses that rely on invalidated /
deleted memory; agents that never forget accumulate stale facts over time.
This harness adopts the metric as a regression: AFTER Loom closes a
validity interval, assert that retrieval at a later event-time NEVER
surfaces the invalidated memory — while a query *inside* the interval
still does.

It exercises the triple/facts plane, where Loom keeps a true
`(valid_from, valid_to)` interval and `facts.list(as_of=…)` filters on
it (server/memory/app/facts.py handle_facts_list). The memory plane's
as-of is lower-bound only, so "invalidated never resurfaces" is a
triple-plane invariant — see bench/temporal_assertions.py.

Write path is deterministic and LLM-FREE: we drive memory.bulk_set
with explicit `relations` + `valid_at` / `valid_to`, which lands typed
triples with the interval already set (server/memory/app/extract/handler.py
write_entity_triples). Closing an interval also fires through
`_repair_contradictory_triples` for single-valued relations, so a
later contradicting write closes the prior open interval the same way
the live extractor would. Use single-valued relations only
(`works_at`, `lives_in`, …) or the closure won't fire.

Run (needs a live control plane + CH):
    python bench/fama_forgetting.py --base-url http://localhost:7777 \
        --token "$LOOM_API_KEY" --out bench/fama_forgetting.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from bench._http import post_json
from bench.bench_namespaces import (
    erase_on_exit_sync,
    register_bench_namespace_sync,
    remember_bench_namespace,
)
from bench.temporal_assertions import forgetting_violation, triple_visible_as_of


# Each case is a single-valued relation whose object changes at
# `closed_at`: the old object's interval becomes [opened_at, closed_at),
# the new object's becomes [closed_at, NULL). `as_of_after` is strictly
# after the close (so the old object MUST be gone); `as_of_within` is
# inside the closed interval (so the old object MUST still surface).
@dataclass(frozen=True)
class ForgetCase:
    case_id: str
    subject: str
    relation: str  # MUST be single-valued (works_at/lives_in/…)
    old_object: str
    new_object: str
    opened_at: str
    closed_at: str
    as_of_within: str
    as_of_after: str


CASES: list[ForgetCase] = [
    ForgetCase(
        case_id="employer-acme-to-clickhouse",
        subject="user",
        relation="works_at",
        old_object="acme",
        new_object="clickhouse",
        opened_at="2022-01-01",
        closed_at="2024-01-01",
        as_of_within="2023-01-01",
        as_of_after="2025-06-01",
    ),
    ForgetCase(
        case_id="residence-berlin-to-amsterdam",
        subject="user",
        relation="lives_in",
        old_object="berlin",
        new_object="amsterdam",
        opened_at="2021-03-01",
        closed_at="2023-09-01",
        as_of_within="2022-01-01",
        as_of_after="2024-01-01",
    ),
]


def _post(
    client: httpx.Client, base_url: str, op: str, body: dict[str, Any], token: str
) -> dict[str, Any]:
    out: dict[str, Any] = post_json(
        client, f"{base_url}/v1/{op}", body, token=token, timeout_s=60.0
    )
    return out


def _bulk_relation(
    client: httpx.Client,
    base_url: str,
    token: str,
    identity: dict[str, str],
    *,
    memory_key: str,
    subject: str,
    relation: str,
    obj: str,
    valid_at: str,
    valid_to: str | None,
) -> None:
    """Land one typed triple via the deterministic bulk_set path."""
    entry: dict[str, Any] = {
        "memory_key": memory_key,
        "value": f"{subject} {relation} {obj}",
        "relations": [[subject, relation, obj]],
        "valid_at": valid_at,
    }
    if valid_to is not None:
        entry["valid_to"] = valid_to
    _post(client, base_url, "memory.bulk_set", {**identity, "entries": [entry]}, token)


def _facts_objects(
    client: httpx.Client,
    base_url: str,
    token: str,
    identity: dict[str, str],
    *,
    subject: str,
    relation: str,
    as_of: str,
    poll_timeout_s: float,
    expect_nonempty: bool,
) -> set[str]:
    """Return the set of objects facts.list reports for (subject,
    relation) as-of `as_of`. Triples are async-inserted, so poll
    briefly when we EXPECT a hit; when expecting emptiness we still
    poll the budget once so the seed has settled."""
    deadline = time.monotonic() + poll_timeout_s
    body = {
        **identity,
        "subject": subject,
        "relation": relation,
        "as_of": as_of,
        "limit": 500,
    }
    last: set[str] = set()
    while time.monotonic() < deadline:
        out = _post(client, base_url, "facts.list", body, token)
        rows = list(out.get("rows") or [])
        last = {str(r.get("object", "")).strip().lower() for r in rows}
        if last and expect_nonempty:
            return last
        if not expect_nonempty:
            # give async inserts a beat, then trust the read
            time.sleep(0.3)
            out = _post(client, base_url, "facts.list", body, token)
            return {str(r.get("object", "")).strip().lower() for r in (out.get("rows") or [])}
        time.sleep(0.2)
    return last


def run_case(
    client: httpx.Client,
    base_url: str,
    token: str,
    case: ForgetCase,
) -> list[dict[str, str]]:
    namespace = f"bench-fama-{uuid.uuid4().hex[:10]}"
    remember_bench_namespace("dev", namespace)
    register_bench_namespace_sync(
        lambda p, b: _post(client, base_url, p.removeprefix("/v1/"), b, token),
        org="dev",
        namespace=namespace,
        description="forgetting case",
    )
    identity = {"org": "dev", "namespace": namespace, "agent": "fama-bench"}

    # 1. Open the old fact, then close it by writing the contradicting
    #    new fact dated at closed_at. The new write closes the prior open
    #    interval via _repair_contradictory_triples (single-valued rel).
    _bulk_relation(
        client,
        base_url,
        token,
        identity,
        memory_key=f"{case.case_id}-old",
        subject=case.subject,
        relation=case.relation,
        obj=case.old_object,
        valid_at=case.opened_at,
        valid_to=None,
    )
    _bulk_relation(
        client,
        base_url,
        token,
        identity,
        memory_key=f"{case.case_id}-new",
        subject=case.subject,
        relation=case.relation,
        obj=case.new_object,
        valid_at=case.closed_at,
        valid_to=None,
    )

    rows: list[dict[str, str]] = []

    # 2. As-of AFTER the close: the invalidated object MUST NOT surface.
    after = _facts_objects(
        client,
        base_url,
        token,
        identity,
        subject=case.subject,
        relation=case.relation,
        as_of=case.as_of_after,
        poll_timeout_s=4.0,
        expect_nonempty=True,
    )
    old_surfaced_after = case.old_object in after
    # Oracle: the old interval is [opened_at, closed_at); after the close
    # it is invisible. Surfacing it is a forgetting violation.
    violation = forgetting_violation(
        valid_from=case.opened_at,
        valid_to=case.closed_at,
        as_of=case.as_of_after,
        surfaced=old_surfaced_after,
    )
    rows.append(
        {
            "case_id": case.case_id,
            "check": "stale_excluded_after_close",
            "as_of": case.as_of_after,
            "expected": "old_object_absent",
            "observed": ",".join(sorted(after)) or "(none)",
            "passed": "0" if violation else "1",
            "forgetting_violation": "1" if violation else "0",
        }
    )

    # 3. As-of WITHIN the old interval: the old fact MUST still surface
    #    (invalidate-not-delete — history stays queryable).
    within = _facts_objects(
        client,
        base_url,
        token,
        identity,
        subject=case.subject,
        relation=case.relation,
        as_of=case.as_of_within,
        poll_timeout_s=4.0,
        expect_nonempty=True,
    )
    old_visible_within = case.old_object in within
    want_within = triple_visible_as_of(case.opened_at, case.closed_at, case.as_of_within)
    rows.append(
        {
            "case_id": case.case_id,
            "check": "history_queryable_within_interval",
            "as_of": case.as_of_within,
            "expected": "old_object_present" if want_within else "old_object_absent",
            "observed": ",".join(sorted(within)) or "(none)",
            "passed": "1" if old_visible_within == want_within else "0",
            "forgetting_violation": "0",
        }
    )

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--token", default="")
    parser.add_argument("--out", default="bench/fama_forgetting.csv")
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
    fieldnames = [
        "case_id",
        "check",
        "as_of",
        "expected",
        "observed",
        "passed",
        "forgetting_violation",
    ]
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    total = len(out_rows)
    passed = sum(1 for r in out_rows if r["passed"] == "1")
    violations = sum(1 for r in out_rows if r["forgetting_violation"] == "1")
    print()
    print(f"{'case / check':<52} {'verdict':>8}")
    for r in out_rows:
        print(
            f"{(r['case_id'] + ' / ' + r['check'])[:52]:<52} "
            f"{('PASS' if r['passed'] == '1' else 'FAIL'):>8}"
        )
    print()
    print(
        f"forgetting: {passed}/{total} checks passed; "
        f"{violations} forgetting violation(s) -> {out_path}"
    )
    # Non-zero exit on any forgetting violation so CI can gate on it.
    return 0 if (passed == total and violations == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
