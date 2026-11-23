"""Preserve code-stage traces and request ledger measurements before cleanup."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from examples.software_factory.factory.model import digest

from .runtime import read, write

logger = logging.getLogger(__name__)

SCRIPT = """import asyncio,json,sys
from server.core.ch import get_client,set_query_tag
async def main():
    params=json.load(sys.stdin)
    client=get_client();await client.connect()
    set_query_tag('bench.factory.harbor.trace_capture')
    try:
        rows=await client.query_all(
            'SELECT org,namespace,trace_run_id,op,route,result_count,tokens_returned,'
            'latency_ms,stage_ms,as_of_commit,lag_commits,reason,error_code '
            'FROM loom.code_search_trace WHERE org={org:String} '
            'AND namespace={namespace:String} AND trace_run_id IN {trace_run_ids:Array(String)}',
            params)
        try:
            requests=await client.query_all(
                "SELECT org,namespace,args['out_trace_run_id'] AS trace_run_id,"
                "operation,request_id,latency_ms,"
                "args['out_stage_ms'] AS stage_ms,"
                "toUInt64OrNull(args['out_lesson_rows']) AS lesson_rows,"
                "toUInt64OrNull(args['out_lesson_chars']) AS lesson_chars,"
                "toUInt64OrNull(args['out_ch_statements']) AS ch_statements,"
                "toUInt64OrNull(args['out_ch_read_rows']) AS ch_rows,"
                "toUInt64OrNull(args['out_ch_read_bytes']) AS ch_bytes,"
                "toFloat64OrNull(args['out_ch_elapsed_ms']) AS ch_elapsed_ms "
                "FROM loom.agent_events WHERE org={org:String} AND namespace={namespace:String} "
                "AND args['out_trace_run_id'] IN {trace_run_ids:Array(String)} AND transport='mcp'",
                params)
            ledger_error=None
        except Exception as error:  # noqa: BLE001 — the ledger must not hide retrieval traces
            requests=[]
            ledger_error=type(error).__name__
        print(json.dumps(dict(rows=rows, requests=requests, ledger_error=ledger_error),default=str))
    finally: await client.disconnect()
asyncio.run(main())
"""


def capture_or_report(protocol, order):
    """Keep optional trace failures separate from grading and billing."""
    try:
        capture(protocol, order)
    except Exception as error:  # noqa: BLE001 — optional diagnostics cannot sink a run
        logger.warning("Code trace capture failed for attempt %s (%s)", order, type(error).__name__)


def capture(protocol, order):
    root = Path(protocol["output_root"])
    directory = root / "diagnostics"
    directory.mkdir(exist_ok=True)
    target = directory / f"attempt-{order:02d}-code-traces.json"
    native = directory / f"attempt-{order:02d}.json"
    summary = read(native) if native.exists() else {}
    ids = sorted(
        {call["trace_run_id"] for call in summary.get("calls", []) if call.get("trace_run_id")}
    )
    identity = {
        "protocol_sha256": digest(protocol),
        "native_source_sha256": summary.get("source_sha256"),
        "requested_trace_run_ids": ids,
    }
    saved = read(target) if target.exists() else None
    if saved and (
        saved["protocol_sha256"] != identity["protocol_sha256"]
        or saved["native_source_sha256"] is not None
        and any(saved.get(key) != value for key, value in identity.items())
    ):
        raise PermissionError("Saved code traces belong to different runtime evidence")
    if saved and saved["complete"] and saved.get("request_ledger_missing_trace_run_ids") == []:
        return saved
    receipt = {
        **identity,
        "complete": False,
        "state": "unavailable",
        "rows": saved["rows"] if saved else [],
        "missing_trace_run_ids": ids,
        "request_ledger": saved.get("request_ledger", []) if saved else [],
        "request_ledger_missing_trace_run_ids": ids,
    }
    if identity["native_source_sha256"] is not None:
        if ids:
            _capture(protocol, order, ids, receipt)
        else:
            receipt.update(complete=True, state="no_trace_ids_returned")
    write(target, receipt)
    return receipt


def _capture(protocol, order, ids, receipt):
    binding = protocol["attempt_scopes"][str(order)]
    try:
        observed = subprocess.run(
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
                    "namespace": binding["code_namespace"],
                    "trace_run_ids": ids,
                }
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        captured = json.loads(observed.stdout)
        rows = _merge_rows(receipt["rows"], _scoped_rows(captured["rows"], binding, ids))
        requests = _merge_rows(
            receipt["request_ledger"], _scoped_rows(captured["requests"], binding, ids)
        )
        receipt["rows"] = rows
        missing = sorted(set(ids) - {row["trace_run_id"] for row in rows})
        receipt.update(
            complete=not missing,
            state="partial" if missing else "captured",
            missing_trace_run_ids=missing,
            request_ledger=requests,
            request_ledger_error=captured["ledger_error"],
            request_ledger_missing_trace_run_ids=sorted(
                set(ids) - {row["trace_run_id"] for row in requests}
            ),
        )
    except (KeyError, OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
        receipt.update(state="capture_error", error_type=type(error).__name__)
        receipt["missing_trace_run_ids"] = sorted(
            set(ids) - {row["trace_run_id"] for row in receipt["rows"]}
        )
        receipt["complete"] = not receipt["missing_trace_run_ids"]
        receipt["request_ledger_missing_trace_run_ids"] = sorted(
            set(ids) - {row["trace_run_id"] for row in receipt["request_ledger"]}
        )


def _scoped_rows(rows, binding, ids):
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or row.get("org") != binding["org"]
        or row.get("namespace") != binding["code_namespace"]
        or row.get("trace_run_id") not in ids
        for row in rows
    ):
        raise PermissionError("Code trace scope differs from the attempt")
    return rows


def _merge_rows(previous, current):
    rows = {(row["trace_run_id"], row.get("request_id", "")): row for row in [*previous, *current]}
    return [rows[key] for key in sorted(rows)]
