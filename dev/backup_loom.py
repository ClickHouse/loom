#!/usr/bin/env python3
"""Loom backup CLI — dumps every Loom table to JSONEachRow files.

Usage:
    python dev/backup_loom.py --out-dir ./backups/<date>
    python dev/backup_loom.py --out-dir ./backups/<date> --org acme

Output:
    {out-dir}/
        <table>.jsonl   — one JSONEachRow file per table
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
    - Every MergeTree-family table of the database is dumped, so a Loom
      restored from the backup gets every feature back. Left out: the
      migration runner's own tables (`_`-prefixed), the KeeperMap leases (live
      coordination, not data) and views (`loom migrate` creates them).
    - `--exclude` also leaves out every table a materialized view fills from
      an excluded one (code_chunks takes code_term_freq and code_doc_stats
      with it): rebuilding the source refills them, so restored copies would
      double. The manifest records each view's source and target table, and
      restore refuses a selection that holds one without the other.
    - `--org` filters every table with an `org` column. A table without one
      (`idempotency`, the signing keys, ...) holds every org's rows, so an
      org-scoped backup skips it rather than carry other tenants' rows.
    - Suitable for cold backup. Hot backup (no-downtime) needs CH BACKUP
      via ClickHouse Cloud or `clickhouse-backup` — out of scope here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev._ch_target import CHTarget, TargetError, add_target_args, resolve_target

# The database's tables a backup carries, each with whether it has an `org` column.
TABLES_SQL = """
SELECT t.name AS name, countIf(c.name = 'org') > 0 AS scoped
FROM system.tables AS t
LEFT JOIN system.columns AS c ON c.database = t.database AND c.table = t.name
WHERE t.database = {db:String} AND t.engine LIKE '%MergeTree'
  AND NOT startsWith(t.name, '_') AND NOT startsWith(t.name, '.')
GROUP BY t.name
ORDER BY t.name
"""
# The tables an insert-fired materialized view reads (`dependencies_table`
# lists it on its source), and each view's CREATE statement for its target.
# A refreshable view is no insert dependency, so it does not appear.
VIEW_SOURCES_SQL = """
SELECT name AS source, arrayJoin(dependencies_table) AS view
FROM system.tables
WHERE database = {db:String} AND notEmpty(dependencies_table)
"""
VIEW_DDL_SQL = """
SELECT name, create_table_query FROM system.tables
WHERE database = {db:String} AND engine = 'MaterializedView'
"""
_VIEW_TARGET = re.compile(r"^\s*CREATE\s+MATERIALIZED\s+VIEW\s+\S+\s+TO\s+(\S+)", re.I)
BACKUP_VERSION = 3
# Tables dumped and restored before the others, in this order: a document row
# before the originals it names, so the blob sweep of a live database never
# finds a restored original that no document names yet.
FIRST_TABLES = ("documents",)


def in_load_order(names: list[str]) -> list[str]:
    """`names` with `FIRST_TABLES` first; the rest keep their order."""
    first = [t for t in FIRST_TABLES if t in names]
    return first + [t for t in names if t not in FIRST_TABLES]


MANIFEST = "manifest.json"
_CHUNK = 1 << 20
# The dump writes String columns as raw bytes (the originals in
# document_blobs are binary). A profile that validates UTF-8 would replace
# them with U+FFFD silently, and the manifest would hash the damage.
_DUMP_SETTINGS = {"output_format_json_validate_utf8": 0}


class DumpError(RuntimeError):
    """The stream ended in something that is not a JSONEachRow row."""


def list_tables(client: Any, database: str) -> dict[str, bool]:
    """table → whether it has an `org` column, for every table a backup carries."""
    rows = client.query(TABLES_SQL, parameters={"db": database}).result_rows
    return {str(name): bool(scoped) for name, scoped in rows}


def dump_table(client: Any, table: str, out_path: Path, org: str | None) -> dict[str, Any]:
    """Stream one table to `out_path`; return its manifest entry
    (file, rows, bytes, sha256). Raises on a query error or a stream that
    does not end in a complete JSON row (ClickHouse appends an exception to
    a body it has already started sending)."""
    where = ""
    params: dict[str, Any] | None = None
    if org is not None:
        where = "WHERE org = {org:String}"
        params = {"org": org}
    sql = f"SELECT * FROM {table} {where} FORMAT JSONEachRow"

    digest = hashlib.sha256()
    size = rows = 0
    pending = last_line = b""  # bytes after the last newline; the last complete line
    stream = client.raw_stream(sql, parameters=params, settings=_DUMP_SETTINGS)
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


def view_edges(client: Any, database: str) -> list[dict[str, str]]:
    """[{view, source, target}] for every insert-fired materialized view of
    the database whose target is one of its tables."""
    sources = client.query(VIEW_SOURCES_SQL, parameters={"db": database}).result_rows
    ddl = dict(client.query(VIEW_DDL_SQL, parameters={"db": database}).result_rows)
    edges: list[dict[str, str]] = []
    for source, view in sources:
        m = _VIEW_TARGET.match(str(ddl.get(view, "")))
        if m is None:
            continue
        target = m.group(1).replace("`", "").rsplit(".", 1)[-1]
        edges.append({"view": str(view), "source": str(source), "target": target})
    return sorted(edges, key=lambda e: (e["source"], e["view"]))


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


def _fed_by(excluded: set[str], edges: list[dict[str, str]]) -> dict[str, str]:
    """table → the excluded table a view fills it from, transitively."""
    fed: dict[str, str] = {}
    frontier = set(excluded)
    while frontier:
        nxt: set[str] = set()
        for e in edges:
            t = e["target"]
            if e["source"] in frontier and t not in excluded and t not in fed:
                fed[t] = e["source"]
                nxt.add(t)
        frontier = nxt
    return fed


def plan_tables(
    available: dict[str, bool],
    selected: str | None,
    org: str | None,
    exclude: str | None = None,
    edges: list[dict[str, str]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """(tables to dump, manifest entries for tables skipped by design).
    Raises ValueError on a name the database does not have."""
    tables = list(available)
    skipped: list[dict[str, Any]] = []
    for arg, keep in ((selected, True), (exclude, False)):
        if not arg:
            continue
        named = {t.strip() for t in arg.split(",") if t.strip()}
        unknown = sorted(named - set(available))
        if unknown:
            raise ValueError(f"unknown table(s) {unknown}; known: {', '.join(available)}")
        if not keep:
            skipped += [
                {"name": t, "skipped": "excluded (--exclude)"} for t in tables if t in named
            ]
            fed = _fed_by(named, edges or [])
            skipped += [
                {"name": t, "skipped": f"filled by a view from excluded {fed[t]}"}
                for t in tables
                if t in fed
            ]
            named |= set(fed)
        tables = [t for t in tables if (t in named) == keep]
    if org is not None:
        for t in [t for t in tables if not available[t]]:
            tables.remove(t)
            skipped.append({"name": t, "skipped": "no org column; not part of an org backup"})
    return in_load_order(tables), skipped


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
    parser = argparse.ArgumentParser(description="Back up every Loom table to NDJSON files.")
    parser.add_argument("--out-dir", required=True, help="Output directory (created if missing)")
    parser.add_argument("--org", default=None, help="Filter to one org (default: all)")
    parser.add_argument("--tables", default=None, help="Comma-separated subset; default: all")
    parser.add_argument(
        "--exclude",
        default=None,
        help="Comma-separated tables to leave out, e.g. ones a pass rebuilds (code_chunks)",
    )
    add_target_args(parser)
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).resolve()
    if (out_dir / MANIFEST).exists():
        print(f"✗ {out_dir} already holds a backup; choose a new directory", file=sys.stderr)
        return 2
    try:
        target = resolve_target(args)
        client = target.client()
    except TargetError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    try:
        try:
            available = list_tables(client, target.database)
            edges = view_edges(client, target.database)
            tables, skipped = plan_tables(available, args.tables, args.org, args.exclude, edges)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
        print(f"Backing up {target.describe()} → {out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = run_backup(client, target, out_dir, tables, skipped, args.org)
        manifest["views"] = edges
        for entry in manifest["tables"]:
            entry.setdefault("scoped", available.get(entry["name"], False))
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
