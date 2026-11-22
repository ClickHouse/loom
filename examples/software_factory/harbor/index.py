"""Fingerprint source rows apart from status caches and memory-link progress."""

from __future__ import annotations

import hashlib
import json
import subprocess

MUTABLE_STATE_KEYS = frozenset(
    {
        "status_snapshot",
        "table_counts",
        "symbol_stats_summary",
        "unit_risk_summary",
        "memory_links_checked",
    }
)
TABLES = (
    "code_chunks",
    "code_symbols",
    "code_edges",
    "code_files",
    "code_index_state",
    "code_caller_guards",
    "code_call_sites_unresolved",
    "code_test_facets",
)


def fingerprint(tables: dict) -> list:
    output = []
    for table in TABLES:
        grouped = {}
        for row in tables[table]:
            if table == "code_index_state" and row["state_key"] in MUTABLE_STATE_KEYS:
                continue
            encoded = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
            grouped.setdefault((row["org"], row["namespace"]), []).append(
                hashlib.sha256(encoded.encode()).hexdigest()
            )
        output.append(
            {
                "table": table,
                "scopes": [
                    {
                        "org": org,
                        "namespace": namespace,
                        "rows": len(values),
                        "sha256": hashlib.sha256("".join(sorted(values)).encode()).hexdigest(),
                    }
                    for (org, namespace), values in sorted(grouped.items())
                ],
            }
        )
    return output


def select_snapshot(full: list, scopes: list[dict]) -> list:
    selected = {(scope["org"], scope["namespace"]) for scope in scopes}
    if len(full) != len(TABLES) or {entry["table"] for entry in full} != set(TABLES):
        raise PermissionError("Source snapshot table set differs")
    return [
        {
            "table": entry["table"],
            "scopes": [
                row for row in entry["scopes"] if (row["org"], row["namespace"]) in selected
            ],
        }
        for entry in full
    ]


def snapshot(container: str, scopes: list[dict]) -> list:
    selected = sorted({(scope["org"], scope["namespace"]) for scope in scopes})
    if any(
        not all(isinstance(value, str) and value and value != "-" for value in scope)
        for scope in selected
    ):
        raise PermissionError("Snapshot requires explicit live code scopes")
    if not selected:
        return fingerprint({table: [] for table in TABLES})
    code = """import asyncio, json
from server.core.ch import get_client, set_query_tag
async def main():
    client=get_client()
    await client.connect()
    set_query_tag('bench.factory.harbor.index_snapshot')
    try:
        tables={}
        for table in TABLES:
            sql=('SELECT * FROM loom.'+table
                 +' FINAL WHERE (org,namespace) IN {scopes:Array(Tuple(String,String))}')
            tables[table]=await client.query_all(sql, {'scopes': SCOPES})
        print(json.dumps(tables, default=str))
    finally:
        await client.disconnect()
asyncio.run(main())
"""
    prefix = "TABLES=" + repr(TABLES) + "\nSCOPES=" + repr(selected) + "\n"
    run = subprocess.run(
        ["docker", "exec", "-i", container, "/opt/venv/bin/python", "-c", prefix + code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return fingerprint(json.loads(run.stdout))
