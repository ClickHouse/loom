"""Mutation-sequence bench over loom.memory_records.

Mirrors the end-to-end mutation shape used by typed-memory systems:

    ingest -> field update -> field clear -> relation change/delete
           -> entity rename -> cascading delete

and measures Loom's reinforcement gate + _repair_contradictory_triples
+ ReplacingMergeTree versioning against exactly these. The typed-record
steps run on the memory.record.* plane; the relation steps run on the
facts/triples plane (the only plane carrying real relations).

WHAT THIS PLANE ACTUALLY SUPPORTS (verified against the live tree):

  loom.memory_records / memory_record_fields are ReplacingMergeTree(version)
  keyed on (org, namespace, schema_name[, field_name], record_id). Each
  memory.record.write mints a NEW record_id, so an "update" is a NEW
  record row under the SAME memory_key; the current state is the latest
  record by updated_at. There is NO memory.record.delete op and the write
  handler always inserts status='active'/tombstoned_at=NULL — so:

    • ingest / field update / field clear (value_state='absent') / entity
      rename are expressible end-to-end and are asserted PASS/FAIL.
    • relation change / relation delete are asserted on the triple plane
      (supersede / close-interval via memory.bulk_set + as_of read).
    • record-level cascading delete has NO primitive today. memory.delete
      tombstones the memory row but does NOT cascade to its linked typed
      record. This bench records what Loom actually does for that step and
      flags it as an expected gap (verdict GAP, not FAIL) rather than
      asserting a pass the API cannot deliver.

LLM-FREE throughout: memory.record.write with explicit fields/states,
memory.bulk_set with explicit relations, memory.delete.

Run (needs a live control plane + CH):
    python bench/mutation_sequence.py --base-url http://localhost:7777 \
        --token "$LOOM_API_KEY" --out bench/mutation_sequence.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
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
from bench.temporal_assertions import step_field_ok

_SCHEMA_FIELDS = [
    {"name": "employer", "type": "string"},
    {"name": "title", "type": "string"},
    {"name": "headcount", "type": "integer"},
]


def _post(
    client: httpx.Client, base_url: str, op: str, body: dict[str, Any], token: str
) -> dict[str, Any]:
    out: dict[str, Any] = post_json(
        client, f"{base_url}/v1/{op}", body, token=token, timeout_s=60.0
    )
    return out


def _write_record(
    client: httpx.Client,
    base_url: str,
    token: str,
    identity: dict[str, str],
    *,
    schema_name: str,
    memory_key: str,
    fields: dict[str, dict[str, Any]],
) -> None:
    """Write one record version. `fields` maps field_name -> {value, state}."""
    _post(
        client,
        base_url,
        "memory.record.write",
        {
            **identity,
            "schema_name": schema_name,
            "memory_key": memory_key,
            "fields": fields,
        },
        token,
    )


def _current_record(
    client: httpx.Client,
    base_url: str,
    token: str,
    identity: dict[str, str],
    *,
    schema_name: str,
    memory_key: str,
    poll_timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Return the latest (current) record row for `memory_key`.

    record.query returns all active records ordered by updated_at DESC;
    we take the freshest row matching the key. record.query has no
    memory_key filter, so we filter client-side."""
    deadline = time.monotonic() + poll_timeout_s
    while time.monotonic() < deadline:
        out = _post(
            client,
            base_url,
            "memory.record.query",
            {
                **identity,
                "schema_name": schema_name,
                "limit": 1000,
            },
            token,
        )
        rows = [r for r in (out.get("rows") or []) if str(r.get("memory_key")) == memory_key]
        if rows:
            return rows[0]  # updated_at DESC -> freshest first
        time.sleep(0.25)
    return {}


def _facts_objects(
    client: httpx.Client,
    base_url: str,
    token: str,
    identity: dict[str, str],
    *,
    subject: str,
    relation: str,
    as_of: str | None,
    poll_timeout_s: float = 5.0,
    until: Any = None,
) -> set[str]:
    """Poll facts.list. Without `until`, returns the first NON-EMPTY object
    set. With `until` (a set-predicate), polls until it holds — interval
    closure is an ALTER UPDATE mutation, i.e. eventually consistent by
    contract, so a converged-state assertion must allow it time to apply
    (first-non-empty returns the pre-mutation superset and flakes)."""
    body: dict[str, Any] = {**identity, "subject": subject, "relation": relation, "limit": 500}
    if as_of is not None:
        body["as_of"] = as_of
    deadline = time.monotonic() + poll_timeout_s
    last: set[str] = set()
    while time.monotonic() < deadline:
        out = _post(client, base_url, "facts.list", body, token)
        last = {str(r.get("object", "")).strip().lower() for r in (out.get("rows") or [])}
        if until is not None:
            if until(last):
                return last
        elif last:
            return last
        time.sleep(0.2)
    return last


def _field_value(record: dict[str, Any], field: str) -> Any:
    return (record.get("record") or {}).get(field)


def _field_state(record: dict[str, Any], field: str) -> str:
    # A field absent from the record's field_states map has never been
    # written; treat as "absent" for the value_state contract.
    return str((record.get("field_states") or {}).get(field, "absent"))


def _row(step: str, expected: str, observed: str, passed: bool | None) -> dict[str, str]:
    verdict = "GAP" if passed is None else ("PASS" if passed else "FAIL")
    return {"step": step, "expected": expected, "observed": observed, "verdict": verdict}


def run(client: httpx.Client, base_url: str, token: str) -> list[dict[str, str]]:
    namespace = f"bench-mutseq-{uuid.uuid4().hex[:10]}"
    remember_bench_namespace("dev", namespace)
    register_bench_namespace_sync(
        lambda p, b: _post(client, base_url, p.removeprefix("/v1/"), b, token),
        org="dev",
        namespace=namespace,
        description="mutation sequence run",
    )
    identity = {"org": "dev", "namespace": namespace, "agent": "mutseq-bench"}
    schema_name = "employment"
    rows: list[dict[str, str]] = []

    _post(
        client,
        base_url,
        "memory_schema.register",
        {
            "org": identity["org"],
            "namespace": namespace,
            "name": schema_name,
            "fields": _SCHEMA_FIELDS,
            "status": "active",
        },
        token,
    )

    # Thin closures so the per-step call sites stay readable.
    def put(memory_key: str, fields: dict[str, dict[str, Any]]) -> None:
        _write_record(
            client,
            base_url,
            token,
            identity,
            schema_name=schema_name,
            memory_key=memory_key,
            fields=fields,
        )

    def cur(memory_key: str, poll_timeout_s: float = 5.0) -> dict[str, Any]:
        return _current_record(
            client,
            base_url,
            token,
            identity,
            schema_name=schema_name,
            memory_key=memory_key,
            poll_timeout_s=poll_timeout_s,
        )

    def fld(rec: dict[str, Any], field: str) -> tuple[Any, str]:
        return _field_value(rec, field), _field_state(rec, field)

    key = "user_employment"

    # 1. INGEST
    put(
        key,
        {
            "employer": {"value": "Acme", "state": "value"},
            "title": {"value": "Engineer", "state": "value"},
            "headcount": {"value": 500, "state": "value"},
        },
    )
    rec = cur(key)
    emp_v, emp_s = fld(rec, "employer")
    hc_v, hc_s = fld(rec, "headcount")
    ok = step_field_ok(
        expected_value="Acme", expected_state="value", observed_value=emp_v, observed_state=emp_s
    ) and step_field_ok(
        expected_value=500, expected_state="value", observed_value=hc_v, observed_state=hc_s
    )
    rows.append(
        _row("ingest", "employer=Acme,headcount=500", f"employer={emp_v},headcount={hc_v}", ok)
    )

    # 2. FIELD UPDATE — change employer + headcount; latest record wins.
    put(
        key,
        {
            "employer": {"value": "ClickHouse", "state": "value"},
            "title": {"value": "Engineer", "state": "value"},
            "headcount": {"value": 1200, "state": "value"},
        },
    )
    rec = cur(key)
    emp_v, emp_s = fld(rec, "employer")
    hc_v, hc_s = fld(rec, "headcount")
    ok = step_field_ok(
        expected_value="ClickHouse",
        expected_state="value",
        observed_value=emp_v,
        observed_state=emp_s,
    ) and step_field_ok(
        expected_value=1200, expected_state="value", observed_value=hc_v, observed_state=hc_s
    )
    rows.append(
        _row(
            "field_update",
            "employer=ClickHouse,headcount=1200",
            f"employer={emp_v},headcount={hc_v}",
            ok,
        )
    )

    # 3. FIELD CLEAR — write the title field at value_state='absent'.
    put(
        key,
        {
            "employer": {"value": "ClickHouse", "state": "value"},
            "title": {"value": None, "state": "absent"},
            "headcount": {"value": 1200, "state": "value"},
        },
    )
    rec = cur(key)
    title_v, title_state = fld(rec, "title")
    ok = step_field_ok(
        expected_value=None,
        expected_state="absent",
        observed_value=title_v,
        observed_state=title_state,
    )
    rows.append(
        _row("field_clear", "title.value_state=absent", f"title.value_state={title_state}", ok)
    )

    # 4. RELATION CHANGE — triple plane: user works_at Acme -> ClickHouse.
    #    The contradicting dated write closes the prior open interval.
    _post(
        client,
        base_url,
        "memory.bulk_set",
        {
            **identity,
            "entries": [
                {
                    "memory_key": "rel-acme",
                    "value": "user works_at acme",
                    "relations": [["user", "works_at", "acme"]],
                    "valid_at": "2020-01-01",
                }
            ],
        },
        token,
    )
    _post(
        client,
        base_url,
        "memory.bulk_set",
        {
            **identity,
            "entries": [
                {
                    "memory_key": "rel-ch",
                    "value": "user works_at clickhouse",
                    "relations": [["user", "works_at", "clickhouse"]],
                    "valid_at": "2024-01-01",
                }
            ],
        },
        token,
    )
    now_objs = _facts_objects(
        client,
        base_url,
        token,
        identity,
        subject="user",
        relation="works_at",
        as_of=None,
        until=lambda objs: objs == {"clickhouse"},
    )
    ok = ("clickhouse" in now_objs) and ("acme" not in now_objs)
    rows.append(
        _row(
            "relation_change",
            "works_at={clickhouse} as-of-now",
            "works_at=" + (",".join(sorted(now_objs)) or "(none)"),
            ok,
        )
    )

    # 5. RELATION DELETE — close the open interval so the relation is
    #    gone as-of-now but the historical window stays queryable.
    #    Writing a contradicting object already closed Acme's interval at
    #    2024-01-01; assert Acme is queryable strictly inside [2020,2024).
    hist_objs = _facts_objects(
        client, base_url, token, identity, subject="user", relation="works_at", as_of="2021-06-01"
    )
    ok = "acme" in hist_objs
    rows.append(
        _row(
            "relation_delete",
            "closed acme queryable as-of 2021-06-01",
            "works_at@2021=" + (",".join(sorted(hist_objs)) or "(none)"),
            ok,
        )
    )

    # 6. ENTITY RENAME — there is no rename primitive; the caller re-keys
    #    the record under a new memory_key. Assert the new key is current
    #    and carries the data.
    new_key = "user_employment_v2"
    put(
        new_key,
        {
            "employer": {"value": "ClickHouse", "state": "value"},
            "title": {"value": "Staff Engineer", "state": "value"},
            "headcount": {"value": 1200, "state": "value"},
        },
    )
    rec_new = cur(new_key)
    title_v, title_s = fld(rec_new, "title")
    ok = step_field_ok(
        expected_value="Staff Engineer",
        expected_state="value",
        observed_value=title_v,
        observed_state=title_s,
    )
    rows.append(
        _row("entity_rename", f"{new_key}.title=Staff Engineer", f"{new_key}.title={title_v}", ok)
    )

    # 7. CASCADING DELETE — memory.delete tombstones the memory row, but
    #    there is NO memory.record.delete and the tombstone does NOT
    #    cascade to the linked typed record. Record what Loom does and
    #    flag it as a GAP (not a FAIL): the record SHOULD disappear from
    #    record.query under true cascading-delete semantics, but today it
    #    persists. This is the honest, observable state.
    _post(client, base_url, "memory.delete", {**identity, "memory_key": new_key}, token)
    rec_after = cur(new_key, poll_timeout_s=2.0)
    record_still_present = bool(rec_after)
    rows.append(
        _row(
            "cascading_delete",
            "record gone after memory.delete (true cascade)",
            "record_still_present=" + ("1" if record_still_present else "0"),
            None if record_still_present else True,
        )
    )

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--token", default="")
    parser.add_argument("--out", default="bench/mutation_sequence.csv")
    args = parser.parse_args()
    token = args.token or os.environ.get("LOOM_API_KEY", "")

    with httpx.Client() as client:
        h = client.get(args.base_url + "/v1/health", timeout=5.0)
        if h.status_code != 200:
            print(f"server unhealthy: {h.status_code}", file=sys.stderr)
            return 2
        # The run's namespace is erased when it exits, after a failure too.
        with erase_on_exit_sync(
            lambda p, b: _post(client, args.base_url, p.removeprefix("/v1/"), b, token),
            org="dev",
        ):
            rows = run(client, args.base_url, token)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["step", "expected", "observed", "verdict"])
        writer.writeheader()
        writer.writerows(rows)

    passed = sum(1 for r in rows if r["verdict"] == "PASS")
    failed = sum(1 for r in rows if r["verdict"] == "FAIL")
    gaps = sum(1 for r in rows if r["verdict"] == "GAP")
    print()
    print(f"{'step':<20} {'verdict':>8}  observed")
    for r in rows:
        print(f"{r['step']:<20} {r['verdict']:>8}  {r['observed']}")
    print()
    print(
        f"mutation-sequence: {passed} pass / {failed} fail / {gaps} gap "
        f"(of {len(rows)}) -> {out_path}"
    )
    # Gaps are known-API-limitations, not regressions; fail only on real FAILs.
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
