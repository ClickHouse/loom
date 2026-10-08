#!/usr/bin/env python3
"""Loom backup CLI — dumps the core Loom tables to JSONEachRow files.

Usage:
    python dev/backup_loom.py --out-dir ./backups/<date>
    python dev/backup_loom.py --out-dir ./backups/<date> --org acme

Output:
    {out-dir}/
        <table>.jsonl   — one JSONEachRow file per table in TABLES
        manifest.json   — written last: the source database, and per table its
                          file, row count, byte size, SHA-256 and CREATE
                          statement, or the error that stopped it

`restore_loom.py` refuses a backup whose manifest is missing, incomplete for
the tables it restores, or whose files do not match their recorded size and
SHA-256.

Exit codes:
    0   every selected table was dumped
    1   at least one table failed (manifest written with `complete: false`)
    2   bad arguments, no ClickHouse target, or the out-dir already holds a backup

Notes:
    - `--org` filters every table with an `org` column. `idempotency` has
      none: its cached responses belong to every org, so an org-scoped backup
      skips it rather than carry other tenants' responses.
    - Derived tables are not dumped: agent_cost_rollup_daily, for example,
      refills from agent_events.
    - Suitable for cold backup. Hot backup (no-downtime) needs CH BACKUP
      via ClickHouse Cloud or `clickhouse-backup` — out of scope here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev._ch_target import CHTarget, TargetError, add_target_args, resolve_target

# Tables to back up, in the order restore_loom.py loads them (it follows the
# manifest). ClickHouse has no FK constraints; the order is for the reader.
TABLES = (
    "namespaces",
    "memory",
    "memory_embeddings",
    "documents",
    "chunks",
    "shared",
    "erasure_requests",
    "agent_events",
    "audit_log",
    "idempotency",
)
# Tables without an `org` column: unfiltered or skipped under --org.
UNSCOPED_TABLES = frozenset({"idempotency"})
BACKUP_VERSION = 2
MANIFEST = "manifest.json"
_CHUNK = 1 << 20


class DumpError(RuntimeError):
    """The stream ended in something that is not a JSONEachRow row."""


def dump_table(client: Any, table: str, out_path: Path, org: str | None) -> dict[str, Any]:
    """Stream one table to `out_path`; return its manifest entry
    (file, rows, bytes, sha256). Raises on a query error or a stream that
    does not end in a complete JSON row (ClickHouse appends an exception to
    a body it has already started sending)."""
    where = ""
    params: dict[str, Any] | None = None
    if org is not None and table not in UNSCOPED_TABLES:
        where = "WHERE org = {org:String}"
        params = {"org": org}
    sql = f"SELECT * FROM {table} {where} FORMAT JSONEachRow"

    digest = hashlib.sha256()
    size = rows = 0
    pending = last_line = b""  # bytes after the last newline; the last complete line
    stream = client.raw_stream(sql, parameters=params)
    try:
        with out_path.open("wb") as f:
            while chunk := stream.read(_CHUNK):
                f.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                rows += chunk.count(b"\n")
                *lines, pending = (pending + chunk).split(b"\n")
                if lines:
                    last_line = lines[-1]
    finally:
        stream.close()
    if pending:
        raise DumpError(f"stream ended mid-row: {pending[:200]!r}")
    if size:
        try:
            # String columns may hold bytes that are not UTF-8 (ClickHouse
            # writes them raw); replacing them keeps the JSON structure intact.
            json.loads(last_line.decode("utf-8", errors="replace"))
        except ValueError as e:
            raise DumpError(f"stream did not end in a JSON row: {last_line[:200]!r}") from e
    return {
        "name": table,
        "file": out_path.name,
        "rows": rows,
        "bytes": size,
        "sha256": digest.hexdigest(),
    }


def table_ddl(client: Any, database: str, table: str) -> str:
    """The table's CREATE statement as the source server reports it; restore
    replays it into a target database that lacks the table."""
    result = client.query(
        "SELECT create_table_query FROM system.tables "
        "WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": table},
    )
    if not result.result_rows:
        raise DumpError(f"no table {database}.{table}")
    return str(result.result_rows[0][0])


def plan_tables(selected: str | None, org: str | None) -> tuple[list[str], list[dict[str, Any]]]:
    """(tables to dump, manifest entries for tables skipped by design).
    Raises ValueError on a name outside TABLES."""
    tables = list(TABLES)
    if selected:
        wanted = [t.strip() for t in selected.split(",") if t.strip()]
        unknown = sorted(set(wanted) - set(TABLES))
        if unknown:
            raise ValueError(f"unknown table(s) {unknown}; known: {', '.join(TABLES)}")
        tables = [t for t in TABLES if t in wanted]
    skipped: list[dict[str, Any]] = []
    if org is not None:
        for t in [t for t in tables if t in UNSCOPED_TABLES]:
            tables.remove(t)
            skipped.append({"name": t, "skipped": "no org column; not part of an org backup"})
    return tables, skipped


def write_manifest(out_dir: Path, manifest: dict[str, Any]) -> Path:
    """Write the manifest last and atomically: a crash mid-backup leaves no
    manifest, which restore treats as no backup."""
    path = out_dir / MANIFEST
    tmp = out_dir / f".{MANIFEST}.tmp"
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, path)
    return path


def run_backup(
    client: Any,
    target: CHTarget,
    out_dir: Path,
    tables: list[str],
    skipped: list[dict[str, Any]],
    org: str | None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for table in tables:
        out_path = out_dir / f"{table}.jsonl"
        try:
            entry = dump_table(client, table, out_path, org)
            entry["create_table_query"] = table_ddl(client, target.database, table)
        except Exception as e:  # noqa: BLE001 — one failed table is recorded; the rest still dump
            print(f"✗ {table}: {e}", file=sys.stderr)
            out_path.unlink(missing_ok=True)
            entries.append({"name": table, "error": str(e)})
            continue
        entries.append(entry)
        print(f"✓ {table}: {entry['rows']} rows")
    for s in skipped:
        print(f"- {s['name']}: skipped ({s['skipped']})")
    return {
        "backup_version": BACKUP_VERSION,
        "backup_at": datetime.now(UTC).isoformat(),
        "source": target.describe(),
        "source_database": target.database,
        "org_filter": org,
        "complete": not any("error" in e for e in entries),
        "tables": entries + skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the core Loom tables to NDJSON files.")
    parser.add_argument("--out-dir", required=True, help="Output directory (created if missing)")
    parser.add_argument("--org", default=None, help="Filter to one org (default: all)")
    parser.add_argument("--tables", default=None, help="Comma-separated subset; default: all known")
    add_target_args(parser)
    args = parser.parse_args(argv)

    try:
        target = resolve_target(args)
        tables, skipped = plan_tables(args.tables, args.org)
    except (TargetError, ValueError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir).resolve()
    if (out_dir / MANIFEST).exists():
        print(f"✗ {out_dir} already holds a backup; choose a new directory", file=sys.stderr)
        return 2

    print(f"Backing up {target.describe()} → {out_dir}")
    try:
        client = target.client()
    except TargetError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest = run_backup(client, target, out_dir, tables, skipped, args.org)
    finally:
        client.close()
    path = write_manifest(out_dir, manifest)
    print(f"\nManifest: {path}")
    if not manifest["complete"]:
        failed = [e["name"] for e in manifest["tables"] if "error" in e]
        print(f"✗ backup incomplete: {', '.join(failed)} failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
