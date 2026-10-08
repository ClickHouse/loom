#!/usr/bin/env python3
"""Loom restore CLI — loads a backup_loom.py backup into a ClickHouse database.

Usage:
    python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --dry-run
    python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore
    python dev/restore_loom.py --in-dir ./backups/<date> --tables memory --into-live

Steps, in order; nothing is written before the last:
    1. read manifest.json (backup_version 2) and select the tables;
    2. verify every selected file's size, row count and SHA-256 against it;
    3. plan against the target database: which tables exist, which will be
       created from the CREATE statement the backup recorded, and how many
       rows each existing table already holds in the backup's scope (its org,
       or the whole table);
    4. refuse the destructive cases unless named explicitly:
         the live database (`loom`, or the database the server config names)
           needs --into-live;
         a table that already holds rows in scope needs --into-nonempty —
           inserts append, so the plain MergeTree tables (audit_log,
           agent_events) would hold every row twice;
    5. create the database and missing tables, then insert into
       `<database>.<table>` (not with --dry-run).

Fidelity: strings, integers, dates and the BFloat16 embeddings round-trip
exactly. ClickHouse's text parser can land a Float32 or Float64 one ULP away
from the value it printed; measured on 26.5 it does so only outside
[0.001, 10), a range Loom's confidence and threshold columns stay inside.

Exit codes:
    0   restored (or, with --dry-run, verified and planned)
    1   a create or insert failed; the report names the tables already inserted
    2   refused: bad arguments, no target, missing or incomplete manifest, a
        file that fails verification, or a destructive case without its flag
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev._ch_target import TargetError, add_target_args, resolve_target
from dev.backup_loom import BACKUP_VERSION, MANIFEST, TABLES, UNSCOPED_TABLES


class Refused(ValueError):
    """The restore cannot proceed; nothing has been written."""


LIVE_DATABASE = "loom"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A restore loads a whole table in one INSERT: a table partitioned by month
# spans every month the backup covers, past the server's 100-partition guard.
_INSERT_SETTINGS = {"max_partitions_per_insert_block": 0}
_CREATE_HEAD = re.compile(r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+?\s*(?=\()", re.I)


@dataclass(frozen=True)
class TablePlan:
    name: str
    path: Path
    rows: int
    ddl: str


def load_manifest(in_dir: Path) -> dict[str, Any]:
    path = in_dir / MANIFEST
    if not path.is_file():
        raise Refused(f"no {MANIFEST} at {path}; a backup without one is incomplete")
    try:
        manifest = json.loads(path.read_text())
    except ValueError as e:
        raise Refused(f"{path} is not valid JSON: {e}") from e
    version = manifest.get("backup_version")
    if version != BACKUP_VERSION:
        raise Refused(
            f"backup_version {version!r} is not {BACKUP_VERSION}: it carries no checksums "
            "to verify; take a new backup with dev/backup_loom.py"
        )
    return manifest


def select_tables(
    manifest: dict[str, Any], tables: str | None, skip_idempotency: bool
) -> list[dict[str, Any]]:
    """The manifest entries to restore, in manifest order. Refuses a
    selected table that failed at backup time or is not in the manifest."""
    entries = [e for e in manifest["tables"] if "skipped" not in e]
    if tables:
        wanted = {t.strip() for t in tables.split(",") if t.strip()}
        missing = sorted(wanted - {e["name"] for e in entries})
        if missing:
            raise Refused(f"not in this backup: {', '.join(missing)}")
        entries = [e for e in entries if e["name"] in wanted]
    if skip_idempotency:
        entries = [e for e in entries if e["name"] != "idempotency"]
    unknown = sorted({e["name"] for e in entries} - set(TABLES))
    if unknown:
        raise Refused(f"manifest names tables this tool does not restore: {unknown}")
    failed = [e["name"] for e in entries if "error" in e]
    if failed:
        raise Refused(
            f"these tables failed at backup time: {', '.join(failed)}; "
            "restore the others with --tables, or take a new backup"
        )
    return entries


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def verify_files(in_dir: Path, entries: list[dict[str, Any]]) -> list[TablePlan]:
    """Check each file against its manifest entry; refuse on any mismatch."""
    plans: list[TablePlan] = []
    for e in entries:
        path = in_dir / e["file"]
        if path.parent != in_dir or not path.is_file():
            raise Refused(f"{e['name']}: file {e['file']!r} missing from {in_dir}")
        size = path.stat().st_size
        if size != e["bytes"]:
            raise Refused(f"{e['name']}: {size} bytes on disk, manifest says {e['bytes']}")
        if _sha256(path) != e["sha256"]:
            raise Refused(f"{e['name']}: SHA-256 does not match the manifest")
        with path.open("rb") as f:
            rows = sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
        if rows != e["rows"]:
            raise Refused(f"{e['name']}: {rows} rows on disk, manifest says {e['rows']}")
        plans.append(
            TablePlan(name=e["name"], path=path, rows=rows, ddl=e.get("create_table_query", ""))
        )
    return plans


def retarget_ddl(ddl: str, database: str, table: str) -> str:
    """The recorded CREATE statement, pointed at `database`.`table` and made
    idempotent. Raises Refused when the statement is not a plain CREATE TABLE."""
    out, n = _CREATE_HEAD.subn(f"CREATE TABLE IF NOT EXISTS `{database}`.`{table}` ", ddl, 1)
    if n != 1:
        raise Refused(f"{table}: the backup's CREATE statement is not a CREATE TABLE")
    return out


@dataclass(frozen=True)
class TargetState:
    database_exists: bool
    missing: tuple[str, ...]  # selected tables the target database lacks
    rows_in_scope: dict[str, int]  # existing tables → rows already in the backup's scope


def inspect_target(
    client: Any, database: str, plans: list[TablePlan], org: str | None
) -> TargetState:
    """Read-only look at the target: does the database exist, which tables
    are missing, how many rows each existing table holds in scope."""
    exists = bool(
        client.query(
            "SELECT count() FROM system.databases WHERE name = {db:String}",
            parameters={"db": database},
        ).result_rows[0][0]
    )
    present: set[str] = set()
    if exists:
        present = {
            row[0]
            for row in client.query(
                "SELECT name FROM system.tables WHERE database = {db:String}",
                parameters={"db": database},
            ).result_rows
        }
    counts: dict[str, int] = {}
    for p in plans:
        if p.name not in present:
            continue
        sql = f"SELECT count() FROM `{database}`.`{p.name}`"
        params: dict[str, Any] | None = None
        if org is not None and p.name not in UNSCOPED_TABLES:
            sql += " WHERE org = {org:String}"
            params = {"org": org}
        counts[p.name] = int(client.query(sql, parameters=params).result_rows[0][0])
    return TargetState(
        database_exists=exists,
        missing=tuple(p.name for p in plans if p.name not in present),
        rows_in_scope=counts,
    )


def check_refusals(
    database: str,
    live_databases: set[str],
    plans: list[TablePlan],
    state: TargetState,
    *,
    into_live: bool,
    into_nonempty: bool,
) -> None:
    """Raise Refused for a destructive case whose flag was not given, or a
    missing table the backup cannot recreate."""
    if database in live_databases and not into_live:
        raise Refused(
            f"{database!r} is the live database; restore into a scratch database "
            "(--database) or pass --into-live"
        )
    nonempty = sorted(t for t, n in state.rows_in_scope.items() if n)
    if nonempty and not into_nonempty:
        raise Refused(
            f"target tables already hold rows in scope: {', '.join(nonempty)}; inserts "
            "append, so restore into an empty database or pass --into-nonempty"
        )
    no_ddl = [p.name for p in plans if p.name in state.missing and not p.ddl]
    if no_ddl:
        raise Refused(
            f"{database!r} lacks {', '.join(no_ddl)} and the backup has no CREATE "
            "statement for it; create the schema first"
        )


def print_plan(target_desc: str, database: str, plans: list[TablePlan], state: TargetState) -> None:
    print(f"Plan: restore into {target_desc}")
    if not state.database_exists:
        print(f"  create database {database}")
    for p in plans:
        have = (
            "create table"
            if p.name in state.missing
            else f"target has {state.rows_in_scope[p.name]} in scope"
        )
        print(f"  {p.name:<20} {p.rows:>10} rows  ({have})")


def execute(
    client: Any, database: str, plans: list[TablePlan], state: TargetState
) -> tuple[list[str], str | None]:
    """Create what is missing, then insert each file in order; stop at the
    first failure. Returns (tables inserted, error or None)."""
    done: list[str] = []
    try:
        if not state.database_exists:
            client.command(f"CREATE DATABASE IF NOT EXISTS `{database}`")
        for p in plans:
            if p.name in state.missing:
                client.command(retarget_ddl(p.ddl, database, p.name))
                print(f"+ created {database}.{p.name}")
    except Exception as e:  # noqa: BLE001 — reported as a failed create, nothing inserted yet
        return done, f"create failed: {e}"
    for p in plans:
        if p.rows == 0:
            continue
        try:
            with p.path.open("rb") as f:
                client.raw_insert(
                    table=f"`{database}`.`{p.name}`",
                    insert_block=f,
                    fmt="JSONEachRow",
                    settings=_INSERT_SETTINGS,
                )
        except Exception as e:  # noqa: BLE001 — reported with the tables already inserted
            return done, f"{p.name}: {e}"
        done.append(p.name)
        print(f"✓ {database}.{p.name}: {p.rows} rows")
    return done, None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore a Loom backup")
    parser.add_argument(
        "--in-dir", required=True, help="Directory containing the backup (with manifest.json)"
    )
    parser.add_argument(
        "--tables", default=None, help="Comma-separated subset; default: all in manifest"
    )
    parser.add_argument(
        "--skip-idempotency",
        action="store_true",
        help="Skip the idempotency cache (recommended on cross-tenant restore)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Verify and plan; write nothing")
    parser.add_argument(
        "--into-live",
        action="store_true",
        help=f"Allow the live database ({LIVE_DATABASE!r} or the configured LOOM_CH_DATABASE)",
    )
    parser.add_argument(
        "--into-nonempty",
        action="store_true",
        help="Allow tables that already hold rows in the backup's scope (rows are appended)",
    )
    add_target_args(parser)
    return parser


def live_databases() -> set[str]:
    """`loom`, plus the database the server configuration names."""
    from server.settings import settings

    return {LIVE_DATABASE, settings.ch_database}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    in_dir = Path(args.in_dir).resolve()
    try:
        target = resolve_target(args)
        if not _IDENT.match(target.database):
            raise Refused(f"database name {target.database!r} is not a plain identifier")
        manifest = load_manifest(in_dir)
        plans = verify_files(in_dir, select_tables(manifest, args.tables, args.skip_idempotency))
        client = target.client(use_database=False)
    except (TargetError, Refused) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    org = manifest.get("org_filter")
    source = manifest.get("source", "?")
    print(f"Backup {in_dir} ({source}, org={org or 'all'}): verified")
    try:
        try:
            state = inspect_target(client, target.database, plans, org)
        except Exception as e:  # noqa: BLE001 — a failed read-only inspection writes nothing
            print(f"✗ could not inspect {target.describe()}: {e}", file=sys.stderr)
            return 2
        print_plan(target.describe(), target.database, plans, state)
        try:
            check_refusals(
                target.database,
                live_databases(),
                plans,
                state,
                into_live=args.into_live,
                into_nonempty=args.into_nonempty,
            )
        except Refused as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
        if args.dry_run:
            print("Dry run: nothing written.")
            return 0
        done, error = execute(client, target.database, plans, state)
    finally:
        client.close()
    total = sum(p.rows for p in plans if p.name in done)
    if error:
        print(f"✗ {error}", file=sys.stderr)
        print(f"✗ partial restore: inserted {done or 'nothing'}; the rest was not", file=sys.stderr)
        return 1
    print(f"\nRestored {total} rows across {len(done)} tables into {target.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
