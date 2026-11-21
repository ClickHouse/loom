"""Join recorded Loom dispatches to the owned server ledger after a complete cohort."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from examples.software_factory.factory.memory_tools import OPS as MEMORY_OPS
from examples.software_factory.factory.model import digest
from examples.software_factory.factory.retrieval_tools import OPERATIONS
from examples.software_factory.harbor.arms import ALL
from examples.software_factory.harbor.runtime import write

FIELDS = (
    "loom_provider_cost_usd",
    "loom_provider_input_tokens",
    "loom_provider_output_tokens",
    "loom_embedding_tokens",
    "loom_response_tokens",
)
READS = OPERATIONS | {"code.index_status"}
MEMORY = set(MEMORY_OPS.values())
LEDGER_SCRIPT = """import asyncio,json,sys
from server.core.ch import get_client,set_query_tag
async def main():
    params=json.load(sys.stdin)
    client=get_client();await client.connect()
    set_query_tag('bench.factory.harbor.provider_billing')
    try:
        rows=await client.query_all(
            'SELECT org,request_id,namespace,operation,success,error_code,input_tokens,'
            'output_tokens,embedding_tokens,upstream_cost_usd,response_tokens '
            'FROM loom.agent_events WHERE org={org:String} '
            'AND request_id IN {ids:Array(String)}',params)
        print(json.dumps(rows,default=str))
    finally: await client.disconnect()
asyncio.run(main())
"""


def load_ledger(protocol: dict, request_ids: list[str]) -> list[dict]:
    """Read only the approved container and exact org/request IDs; never scan a tenant."""
    container = protocol["server_container"]
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        raise ValueError("A frozen owned server container name is required")
    result = subprocess.run(
        ["docker", "exec", "-i", container, "/opt/venv/bin/python", "-c", LEDGER_SCRIPT],
        input=json.dumps({"org": protocol["org"], "ids": request_ids}),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Malformed provider ledger response")
    return rows


def _unknown(reason, *, dispatched=None, matched=0):
    return {
        **dict.fromkeys(FIELDS),
        "physical_requests_dispatched": dispatched,
        "physical_requests_matched": matched,
        "proof_status": "unknown",
        "reason": reason,
    }


def _trace(protocol, slot, result, *, rows=None):
    if not isinstance(result, dict):
        raise ValueError("Attempt result metadata is missing")
    if type(result.get("memory_enabled")) is not bool or any(
        result.get(key) != value
        for key, value in {
            "protocol_sha256": digest(protocol),
            "task_id": slot["task_id"],
            "mode": slot["mode"],
            "arm": slot["arm"],
            "index_tier": slot["index_tier"],
            "memory_enabled": slot["memory_enabled"],
        }.items()
    ):
        raise ValueError("Attempt identity or protocol is unverified")
    arm = ALL[slot["arm"]]
    if type(slot.get("memory_enabled")) is not bool or any(
        slot.get(key) != value for key, value in arm.identity().items()
    ):
        raise ValueError("Attempt treatment is unverified")
    binding = protocol["attempt_scopes"].get(str(slot["order"]))
    if slot["mode"] == "baseline":
        if not isinstance(binding, dict) or any(
            binding.get(key) != value
            for key, value in {
                "org": "-",
                "code_namespace": "-",
                "memory_namespace": None,
                "token_file": None,
                "index_tier": None,
            }.items()
        ):
            raise ValueError("Baseline must not have a live Loom scope")
        binding = None
    elif (
        not isinstance(binding, dict)
        or binding.get("task_id") != slot["task_id"]
        or binding.get("index_tier") != slot["index_tier"]
        or any(
            not isinstance(binding.get(key), str) or not binding[key]
            for key in ("org", "code_namespace")
        )
        or (isinstance(binding.get("memory_namespace"), str) and bool(binding["memory_namespace"]))
        != slot["memory_enabled"]
        or (not slot["memory_enabled"] and binding.get("memory_namespace") is not None)
    ):
        raise ValueError("Attempt scope is unverified")
    if rows is None:
        path = Path(result["harbor_trial"]) / "agent/loom-transport.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Malformed transport event")
    if (slot["mode"] == "baseline") != (slot["index_tier"] is None):
        raise ValueError("Attempt tier is unverified")
    pending, calls = None, []
    for row in rows:
        if row.get("phase") == "dispatch" and pending is None:
            pending = row
        elif row.get("phase") == "response" and pending is not None:
            request_id = row.get("request_id")
            if (
                type(row.get("status")) is not int
                or row["status"] != 200
                or not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", request_id)
                or not isinstance(row.get("path"), str)
                or row.get("path") != pending.get("path")
                or pending.get("request_id") not in {None, request_id}
            ):
                raise ValueError("Failed, missing or mismatched physical response")
            operation = row["path"].removeprefix("/v1/")
            allowed = READS | (MEMORY if slot["memory_enabled"] else set())
            if not row["path"].startswith("/v1/") or operation not in allowed:
                raise ValueError("Transport contains an undeclared operation")
            calls.append((request_id, operation))
            pending = None
        else:
            raise ValueError("Unpaired or malformed transport event")
    if pending is not None:
        raise ValueError("A physical dispatch has no response")
    count = len(calls)
    if any(
        type(result.get(key)) is not int or result[key] != count
        for key in ("loom_physical_requests", "loom_transport_dispatches")
    ):
        raise ValueError("Physical dispatch counts are missing or disagree")
    if count == 0:
        if (
            type(result.get("loom_logical_requests")) is not int
            or result["loom_logical_requests"] != 0
        ):
            raise ValueError("Zero exposure is unverified")
    elif slot["mode"] == "baseline":
        raise ValueError("Baseline contains Loom dispatches")
    return calls, binding


def _price(calls, binding, grouped):
    matched, amounts = 0, []
    for request_id, operation in calls:
        records = grouped.get(request_id, [])
        if len(records) != 1:
            return _unknown(
                "Missing or duplicate ledger rows", dispatched=len(calls), matched=matched
            )
        row = records[0]
        if (
            row.get("org") != binding["org"]
            or row.get("namespace")
            != binding["memory_namespace" if operation in MEMORY else "code_namespace"]
            or row.get("operation") != operation
            or row.get("success") is not True
            or row.get("error_code")
        ):
            return _unknown(
                "Ledger scope, operation or success differs", dispatched=len(calls), matched=matched
            )
        matched += 1
        keys = ("input_tokens", "output_tokens", "embedding_tokens", "response_tokens")
        if any(type(row.get(key)) is not int or row[key] < 0 for key in keys):
            return _unknown(
                "Provider token usage is incomplete", dispatched=len(calls), matched=matched
            )
        try:
            value = row["upstream_cost_usd"]
            if isinstance(value, bool):
                raise ValueError("Boolean cost")
            cost = Decimal(str(value))
            if not cost.is_finite() or cost < 0:
                raise ValueError("Invalid cost")
        except (KeyError, InvalidOperation, ValueError):
            return _unknown("Provider cost is incomplete", dispatched=len(calls), matched=matched)
        amounts.append((cost, *[row[key] for key in keys]))
    totals = [sum(values) for values in zip(*amounts, strict=True)] if amounts else [0] * 5
    return {
        **dict(zip(FIELDS, [float(totals[0]), *totals[1:]], strict=True)),
        "physical_requests_dispatched": len(calls),
        "physical_requests_matched": matched,
        "physical_request_ids_sha256": digest(sorted(identifier for identifier, _ in calls)),
        "proof_status": "known",
        "reason": "Complete scoped ledger join" if calls else "Explicit verified zero dispatches",
    }


def saved_ledger_loader(protocol):
    root = Path(protocol["output_root"])
    path = root / "provider-ledger.json"
    costs = json.loads((root / "provider-costs.json").read_text())
    saved = json.loads(path.read_text())
    if (
        saved.get("protocol_sha256") != digest(protocol)
        or costs.get("protocol_sha256") != digest(protocol)
        or hashlib.sha256(path.read_bytes()).hexdigest() != costs.get("provider_ledger_sha256")
    ):
        raise ValueError("Saved provider ledger differs from its cost receipt")

    def load(scoped, identifiers):
        matches = [row for row in saved["queries"] if row["org"] == scoped["org"]]
        if len(matches) != 1 or matches[0]["request_ids"] != identifiers:
            raise ValueError("Saved provider ledger scope or requested IDs differ")
        return matches[0]["rows"]

    return load


def audit_provider_costs(protocol, entries, *, ledger_loader=None):
    """Write one batch audit. Unknown usage stays unknown; this does not authorize inference."""
    if ledger_loader is None:
        ledger_loader = (
            saved_ledger_loader(protocol)
            if (Path(protocol["output_root"]) / "provider-ledger.json").exists()
            else load_ledger
        )
    schedule = protocol["schedule"]
    orders = [entry.get("order") for entry in entries]
    complete = (
        len(entries) == len(schedule)
        and all(type(n) is int for n in orders)
        and set(orders) == set(range(len(schedule)))
        and all(slot.get("order") == n for n, slot in enumerate(schedule))
    )
    proof = {str(slot["order"]): _unknown("Cohort is incomplete") for slot in schedule}
    traces = {}
    if complete:
        for entry in entries:
            order = entry["order"]
            try:
                traces[order] = _trace(protocol, schedule[order], entry["result"])
            except (KeyError, ValueError, TypeError, OSError) as error:
                proof[str(order)] = _unknown(str(error))
    occurrences = Counter(request_id for calls, _ in traces.values() for request_id, _ in calls)
    for order, (calls, _) in list(traces.items()):
        if any(occurrences[request_id] != 1 for request_id, _ in calls):
            proof[str(order)] = _unknown(
                "Duplicate physical response request ID", dispatched=len(calls)
            )
            del traces[order]
    ids = sorted({request_id for calls, _ in traces.values() for request_id, _ in calls})
    by_org = defaultdict(list)
    for calls, binding in traces.values():
        if calls:
            by_org[binding["org"]].extend(identifier for identifier, _ in calls)
    grouped, query_status, receipts = defaultdict(list), "not_needed", []
    if ids:
        try:
            rows = []
            for org, identifiers in sorted(by_org.items()):
                batch = ledger_loader({**protocol, "org": org}, sorted(identifiers))
                if not isinstance(batch, list) or any(
                    not isinstance(row, dict)
                    or row.get("org") != org
                    or row.get("request_id") not in identifiers
                    for row in batch
                ):
                    raise ValueError("Ledger returned malformed, unrequested or foreign rows")
                rows.extend(batch)
                receipts.append({"org": org, "request_ids": sorted(identifiers), "rows": batch})
            for row in rows:
                grouped[row["request_id"]].append(row)
            query_status = "completed"
        except Exception as error:  # noqa: BLE001 — failed billing reads retain unknown cost
            query_status = "failed:" + type(error).__name__
    for order, (calls, binding) in traces.items():
        proof[str(order)] = (
            _unknown("Provider ledger query failed", dispatched=len(calls))
            if calls and query_status.startswith("failed:")
            else _price(calls, binding, grouped)
        )
    receipt = {
        "schema_version": 1,
        "protocol_sha256": digest(protocol),
        "cohort_complete": complete,
        "ledger_query_status": query_status,
        "request_ids_requested": len(ids),
        "attempts": proof,
        "limitations": [
            "Recorded ledger provider usage/cost; defaults can be zero and are not invoices.",
            "Response/context tokens stay separate from provider usage and model input.",
            "Infrastructure cost and total invoice reconciliation remain unknown.",
            "The audit uses SELECT and writes only its local receipt.",
        ],
    }
    if complete and query_status in {"completed", "not_needed"}:
        path = Path(protocol["output_root"]) / "provider-ledger.json"
        write(path, {"schema_version": 1, "protocol_sha256": digest(protocol), "queries": receipts})
        receipt["provider_ledger_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    write(Path(protocol["output_root"]) / "provider-costs.json", receipt)
    return receipt
