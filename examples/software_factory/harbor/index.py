"""Fingerprint source index rows while excluding explicit status read caches."""

from __future__ import annotations

import hashlib
import json
import subprocess

STATUS_CACHES = frozenset(
    {
        "status_snapshot",
        "table_counts",
        "symbol_stats_summary",
        "unit_risk_summary",
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
            if table == "code_index_state" and row["state_key"] in STATUS_CACHES:
                continue
            encoded = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
            grouped.setdefault(row["namespace"], []).append(
                hashlib.sha256(encoded.encode()).hexdigest()
            )
        output.append(
            {
                "table": table,
                "namespaces": {
                    ns: {
                        "rows": len(values),
                        "sha256": hashlib.sha256("".join(sorted(values)).encode()).hexdigest(),
                    }
                    for ns, values in sorted(grouped.items())
                },
            }
        )
    return output


def snapshot(container: str, org: str) -> list:
    code = """import asyncio, json
from server.core.ch import get_client, set_query_tag
async def main():
    client=get_client()
    await client.connect()
    set_query_tag('bench.factory.harbor.index_snapshot')
    try:
        tables={}
        for table in TABLES:
            sql='SELECT * FROM loom.'+table+' FINAL WHERE org={org:String}'
            tables[table]=await client.query_all(sql, {'org': ORG})
        print(json.dumps(tables, default=str))
    finally:
        await client.disconnect()
asyncio.run(main())
"""
    prefix = "TABLES=" + repr(TABLES) + "\nORG=" + repr(org) + "\n"
    run = subprocess.run(
        ["docker", "exec", "-i", container, "/opt/venv/bin/python", "-c", prefix + code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return fingerprint(json.loads(run.stdout))
