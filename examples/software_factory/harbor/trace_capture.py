"""Preserve existing code-stage traces before the private index is removed."""

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
        print(json.dumps(rows,default=str))
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
    if saved and saved["complete"]:
        return saved
    receipt = {
        **identity,
        "complete": False,
        "state": "unavailable",
        "rows": saved["rows"] if saved else [],
        "missing_trace_run_ids": ids,
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
        rows = json.loads(observed.stdout)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or row.get("org") != binding["org"]
            or row.get("namespace") != binding["code_namespace"]
            or row.get("trace_run_id") not in ids
            for row in rows
        ):
            raise PermissionError("Code trace scope differs from the attempt")
        receipt["rows"] = rows
        missing = sorted(set(ids) - {row["trace_run_id"] for row in rows})
        receipt.update(
            complete=not missing,
            state="partial" if missing else "captured",
            missing_trace_run_ids=missing,
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
        receipt.update(state="capture_error", error_type=type(error).__name__)
        receipt["missing_trace_run_ids"] = sorted(
            set(ids) - {row["trace_run_id"] for row in receipt["rows"]}
        )
