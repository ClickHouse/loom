#!/usr/bin/env python3
"""Audit log integrity verifier.

Walks the rows of `audit_log` in the target database and recomputes each
`row_hash` from the row's canonical content (`row_matches_hash`, the check the
`audit_verifier` worker runs). Reports rows whose stored hash doesn't match.

Per-row tamper detection only — see `server/observability/audit.py`
module docstring for why we don't claim a chain.

A reported mismatch names the row by partition, ts and request_id with its
stored hash; it never prints the row's actor, subject or namespace.

Exit codes:
    0   every row in the window passes (zero rows is a pass)
    1   at least one row's hash does not match (see stderr, or --json)
    2   could not verify: bad arguments, no target, or the query failed

Usage:
    python dev/verify_audit_chain.py [--org X] [--ts-since YYYY-MM-DD] [--json]
"""

# ruff: noqa: E402  — server.* imports need repo root on the path first

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dev._ch_target import TargetError, add_target_args, resolve_target
from server.observability.audit import _to_str, row_matches_hash


def verify(client: Any, *, org: str | None, since: datetime | None) -> tuple[int, list[dict]]:
    """(rows checked, mismatching rows) for the window."""
    where = ["1=1"]
    params: dict[str, object] = {}
    if org is not None:
        where.append("org = {org:String}")
        params["org"] = org
    if since is not None:
        where.append("ts >= toDateTime64({since:String}, 3, 'UTC')")
        params["since"] = since.isoformat()

    result = client.query(
        f"""
        SELECT toYYYYMM(ts) AS partition, ts, event_type, org, namespace,
               actor, subject, request_id, row_hash
        FROM audit_log
        WHERE {" AND ".join(where)}
        ORDER BY partition, ts, request_id
        """,
        parameters=params,
    )
    cols = result.column_names
    breaks: list[dict] = []
    rows_checked = 0
    for raw in result.result_rows:
        row = dict(zip(cols, raw, strict=True))
        rows_checked += 1
        if row_matches_hash(row):
            continue
        breaks.append(
            {
                "partition": int(row["partition"]),
                "ts": row["ts"].isoformat(),
                "request_id": row["request_id"],
                "stored_row_hash": _to_str(row["row_hash"]).rstrip("\x00"),
                "reason": "row_hash mismatch",
            }
        )
    return rows_checked, breaks


def report(checked: int, breaks: list[dict], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"rows_checked": checked, "breaks": breaks, "ok": not breaks}))
    elif not breaks:
        print(f"✓ all rows pass: {checked} verified")
    else:
        print(f"✗ {len(breaks)} tampered row(s) across {checked} rows", file=sys.stderr)
        for b in breaks[:5]:
            print(json.dumps(b), file=sys.stderr)
        if len(breaks) > 5:
            print(f"... +{len(breaks) - 5} more", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify audit_log row hashes.")
    parser.add_argument("--org", help="Restrict to one org")
    parser.add_argument("--ts-since", help="ISO datetime; only rows newer")
    add_target_args(parser)
    parser.add_argument("--json", action="store_true", help="Emit JSON summary on stdout")
    args = parser.parse_args(argv)

    try:
        since = datetime.fromisoformat(args.ts_since) if args.ts_since else None
        target = resolve_target(args)
        client = target.client()
    except (TargetError, ValueError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    try:
        checked, breaks = verify(client, org=args.org, since=since)
    except Exception as e:  # noqa: BLE001 — a failed read is "could not verify", never a pass or a mismatch
        print(f"✗ could not read audit_log on {target.describe()}: {e}", file=sys.stderr)
        return 2
    finally:
        client.close()

    report(checked, breaks, as_json=args.json)
    return 0 if not breaks else 1


if __name__ == "__main__":
    raise SystemExit(main())
