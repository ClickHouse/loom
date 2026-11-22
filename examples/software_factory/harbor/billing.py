"""Attribute native MCP provider usage by the attempt's authenticated subject."""

from __future__ import annotations

import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path

from examples.software_factory.factory.model import digest

from .runtime import read, write

FIELDS = (
    "loom_provider_cost_usd",
    "loom_provider_input_tokens",
    "loom_provider_output_tokens",
    "loom_embedding_tokens",
    "loom_response_tokens",
)
SCRIPT = """import asyncio,json,sys
from server.core.ch import get_client,set_query_tag
async def main():
    params=json.load(sys.stdin)
    client=get_client();await client.connect()
    set_query_tag('bench.factory.harbor.native_billing')
    try:
        rows=await client.query_all(
            'SELECT org,namespace,subject,request_id,operation,success,error_code,'
            'input_tokens,output_tokens,embedding_tokens,upstream_cost_usd,response_tokens '
            'FROM loom.agent_events WHERE org={org:String} AND subject={subject:String}',params)
        written = 0
        if params['memory_namespace']:
            from server.memory.domain.trust import actor_for
            memory_params = dict(params, actor=actor_for(params['subject'],is_service_account=True))
            found = await client.query_all(
                'SELECT uniqExact(tuple(memory_id,version)) AS written FROM loom.memory '
                'WHERE org={org:String} AND namespace={memory_namespace:String} '
                'AND actor={actor:String}',memory_params)
            written = found[0]['written']
        print(json.dumps({'events':rows,'memory_rows_written':written},default=str))
    finally: await client.disconnect()
asyncio.run(main())
"""


def load_ledger(protocol, binding):
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            protocol["server_container"],
            "/opt/venv/bin/python",
            "-c",
            SCRIPT,
        ],
        input=json.dumps(
            {
                "org": binding["org"],
                "subject": binding["token_subject"],
                "memory_namespace": binding["memory_namespace"] or "",
            }
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def audit_provider_costs(protocol, entries, *, ledger_loader=None):
    root = Path(protocol["output_root"])
    saved_path = root / "provider-ledger.json"
    saved = read(saved_path) if saved_path.exists() else None
    costs_path = root / "provider-costs.json"
    if saved and (
        not costs_path.exists()
        or read(costs_path).get("provider_ledger_sha256")
        != hashlib.sha256(saved_path.read_bytes()).hexdigest()
    ):
        raise PermissionError("Saved provider ledger differs from its receipt")
    if saved and saved["protocol_sha256"] != digest(protocol):
        raise PermissionError("Provider ledger belongs to another protocol")
    proofs, queries = {}, {}
    for entry in entries:
        order, result = entry["order"], entry["result"]
        slot = protocol["schedule"][order]
        binding = protocol["attempt_scopes"][str(order)]
        proof = {**dict.fromkeys(FIELDS), "proof_status": "unknown"}
        try:
            if slot["mode"] == "baseline":
                if result.get("loom_tool_calls") != 0:
                    raise ValueError("Baseline exposure is unknown")
                rows, written = [], 0
            else:
                if saved and str(order) in saved["attempts"]:
                    observed = saved["attempts"][str(order)]
                else:
                    observed = (ledger_loader or load_ledger)(protocol, binding)
                rows, written = observed["events"], observed["memory_rows_written"]
                if not isinstance(rows, list) or any(
                    row.get("org") != binding["org"]
                    or row.get("subject") != binding["token_subject"]
                    or (
                        row.get("namespace")
                        not in {binding["code_namespace"], binding["memory_namespace"]}
                        and not (
                            row.get("operation") == "mcp.initialize"
                            and row.get("namespace") == "-"
                            and not slot.get("memory_enabled", False)
                        )
                    )
                    for row in rows
                ):
                    raise ValueError("Native ledger scope differs")
                operations = [row for row in rows if row.get("operation") != "mcp.initialize"]
                if (
                    result.get("loom_tool_calls") is None
                    or len(operations) < result["loom_tool_calls"]
                ):
                    raise ValueError("Native tool calls exceed attributed ledger rows")
            ids = [row["request_id"] for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate ledger request IDs")
            totals = [Decimal(0), 0, 0, 0, 0]
            for row in rows:
                cost = Decimal(str(row["upstream_cost_usd"]))
                tokens = [
                    row[k]
                    for k in (
                        "input_tokens",
                        "output_tokens",
                        "embedding_tokens",
                        "response_tokens",
                    )
                ]
                if (
                    not cost.is_finite()
                    or cost < 0
                    or any(type(v) is not int or v < 0 for v in tokens)
                ):
                    raise ValueError("Invalid provider usage")
                totals = [a + b for a, b in zip(totals, [cost, *tokens], strict=True)]
            proof.update(dict(zip(FIELDS, [float(totals[0]), *totals[1:]], strict=True)))
            proof.update(
                proof_status="known",
                physical_requests_matched=len(rows),
                reason="Authenticated attempt subject; includes failed operations",
            )
            proof["memory_rows_written"] = written
            queries[str(order)] = {"events": rows, "memory_rows_written": written}
        except Exception as error:  # noqa: BLE001 — incomplete billing remains unknown
            proof["reason"] = type(error).__name__
        proofs[str(order)] = proof
    ledger = {"protocol_sha256": digest(protocol), "attempts": queries}
    if not saved or saved != ledger:
        write(saved_path, ledger)
    receipt = {
        "protocol_sha256": digest(protocol),
        "provider_ledger_sha256": hashlib.sha256(saved_path.read_bytes()).hexdigest(),
        "attempts": proofs,
        "cohort_complete": len(entries) == len(protocol["schedule"]),
        "limitations": [
            "Native ledger usage is reported usage, not invoice reconciliation.",
            "Setup and posthoc judging are separate from agent cost.",
        ],
    }
    write(root / "provider-costs.json", receipt)
    return receipt
