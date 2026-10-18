#!/usr/bin/env python3
"""Loom restore CLI — loads a backup_loom.py backup into a ClickHouse database.

Usage:
    python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --dry-run
    python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore
    python dev/restore_loom.py --in-dir ./backups/<date> \
        --tables memory,memory_versions,memory_code_link_queue --into-live

Steps, in order; nothing is written before the last:
    1. read manifest.json (backup_version 3) and select the tables;
    2. verify every selected file's size, row count and SHA-256 against it;
    3. plan against the target database: which tables exist, which will be
       created from the CREATE statement the backup recorded, and how many
       rows each existing table already holds in the backup's scope (its org,
       or the whole table);
    4. refuse a selection that splits a materialized view: an attach fires no
       view, so a source restored without its target leaves the target
       without the restored rows (memory without memory_versions: history,
       redact and rollback cannot see them), and a target restored without
       its source is filled again when the source is rebuilt (code_term_freq
       without code_chunks doubles after a re-ingest). `documents` and
       `document_blobs` count as one pair too (documents without their
       originals answer 410). --allow-view-split accepts it, for a target that
       already holds the rows;
    5. refuse the destructive cases unless named explicitly:
         the live database (`loom`, or the database the server config names)
           needs --into-live;
         a table that already holds rows in scope needs --into-nonempty —
           inserts append, so the plain MergeTree tables (audit_log,
           agent_events) would hold every row twice;
    6. create the database and missing tables, then insert into
       `<database>.<table>` through a staging table whose parts attach to it,
       so no materialized view fires on a restored row (not with --dry-run);
       a replicated table outside a Replicated database stages in its local
       engine, so the copy claims no Keeper path. Each run names its staging
       tables `_restore_<run id>_<table>` and prints the id, so concurrent
       restores into one database never share one; a crashed run's tables
       stay behind, and `--drop-staging <run id>` removes them.
       One run at a time writes into a database: a run holds
       `default._loom_restore_lock_<database>` (a table whose creation is the
       claim) from a fresh look at the target through its last attach, so the
       refusals of step 5 judge what the previous run left, not what was there
       before it.
       The insert keeps every row as dumped (optimize_on_insert = 0): a
       ReplacingMergeTree or AggregatingMergeTree table collapses rows of one
       key at its merges, as the source did, not inside the restore.

Each table prints `… attaching <table>` before its attach and `✓` after it,
so the last line of an interrupted run names the table it was in.

Fidelity: strings, integers, dates and the BFloat16 embeddings round-trip
exactly. ClickHouse's text parser can land a Float32 or Float64 one ULP away
from the value it printed; measured on 26.5 it does so only outside
[0.001, 10), a range Loom's confidence and threshold columns stay inside.

Exit codes:
    0   restored (or, with --dry-run, verified and planned)
    1   a create or insert failed; the report names the tables already inserted
    2   refused: bad arguments, no target, missing or incomplete manifest, a
        file that fails verification, a selection that splits a view, a
        destructive case without its flag, or another run holding the
        database's restore lock past its wait
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev._ch_target import TargetError, add_target_args, resolve_target
from dev.backup_loom import BACKUP_VERSION, MANIFEST, in_load_order


class Refused(ValueError):
    """The restore cannot proceed; nothing has been written."""


LIVE_DATABASE = "loom"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A restore loads a whole table in one INSERT: a table partitioned by month
# spans every month the backup covers, past the server's 100-partition guard.
# optimize_on_insert = 0: the dump holds every row of every part, duplicates of
# a key included, and the restored table must hold the same rows.
_INSERT_SETTINGS = {"max_partitions_per_insert_block": 0, "optimize_on_insert": 0}
_RUN_ID = re.compile(r"^[0-9a-f]{8}$")
# How long a run waits for another run's claim on its target database.
_LOCK_WAIT_S = 300.0
_CREATE_HEAD = re.compile(r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\S+?\s*(?=\()", re.I)


@dataclass(frozen=True)
class TablePlan:
    name: str
    path: Path
    rows: int
    ddl: str
    # Whether the table has an `org` column (an org backup filtered it).
    scoped: bool = True


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
            f"backup_version {version!r} is not {BACKUP_VERSION}; take a new backup with "
            "dev/backup_loom.py"
        )
    if "views" not in manifest:
        raise Refused(f"{path} lists no views; the backup is incomplete")
    return manifest


def _names(arg: str | None) -> set[str]:
    return {t.strip() for t in (arg or "").split(",") if t.strip()}


def select_tables(
    manifest: dict[str, Any],
    tables: str | None,
    skip_idempotency: bool,
    exclude: str | None = None,
) -> list[dict[str, Any]]:
    """The manifest entries to restore, in manifest order with `FIRST_TABLES`
    first. Refuses a selected table that failed at backup time or is not in the manifest."""
    entries = [e for e in manifest["tables"] if "skipped" not in e]
    for arg, keep in ((tables, True), (exclude, False)):
        named = _names(arg)
        if not named:
            continue
        missing = sorted(named - {e["name"] for e in entries})
        if missing:
            raise Refused(f"not in this backup: {', '.join(missing)}")
        entries = [e for e in entries if (e["name"] in named) == keep]
    if skip_idempotency:
        entries = [e for e in entries if e["name"] != "idempotency"]
    failed = [e["name"] for e in entries if "error" in e]
    if failed:
        raise Refused(
            f"these tables failed at backup time: {', '.join(failed)}; "
            "restore the others with --tables, or take a new backup"
        )
    by_name = {e["name"]: e for e in entries}
    return [by_name[t] for t in in_load_order(list(by_name))]


# Tables no view joins that are restored together all the same: (table, partner, why).
_PAIRED = (
    ("documents", "document_blobs", "every original a restored document names answers 410"),
    ("document_blobs", "documents", "the sweep deletes every original no document names"),
)


def check_view_split(plans: list[TablePlan], edges: list[dict[str, str]]) -> None:
    """Raise Refused when the selection holds one end of a view but not the other,
    or a document table without its partner."""
    chosen = {p.name for p in plans}
    problems = [
        f"{a} without {b}: {why}" for a, b, why in _PAIRED if a in chosen and b not in chosen
    ]
    for e in edges:
        src, tgt, view = e["source"], e["target"], e["view"]
        if src in chosen and tgt not in chosen:
            problems.append(f"{src} without {tgt}: {view} copies no restored {src} row into {tgt}")
        elif tgt in chosen and src not in chosen:
            problems.append(f"{tgt} without {src}: {view} fills {tgt} again when {src} is rebuilt")
    if problems:
        raise Refused(
            "the selection splits a materialized view (an attach fires none) or a "
            "document table from its partner:\n  "
            + "\n  ".join(problems)
            + "\nrestore both ends, or pass --allow-view-split when the target already "
            "holds these rows"
        )


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
            TablePlan(
                name=e["name"],
                path=path,
                rows=rows,
                ddl=e.get("create_table_query", ""),
                scoped=bool(e.get("scoped", True)),
            )
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
        if org is not None and p.scoped:
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


# A replicated engine and its Keeper path and replica name, when written out.
_REPLICATED = re.compile(
    r"\bReplicated(\w*MergeTree)\(\s*(?:'(?:[^'\\]|\\.)*'\s*,\s*'(?:[^'\\]|\\.)*'\s*,?\s*)?"
)


# The written-out path and replica name of a replicated engine, as groups, and
# whether more engine arguments follow (a version column).
_REPLICA_ARGS = re.compile(
    r"(\bReplicated\w*MergeTree\()\s*'((?:[^'\\]|\\.)*)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*(,?)\s*"
)
_MACRO = re.compile(r"\{(\w+)\}")
# Macros ClickHouse fills in for each table itself.
_TABLE_MACROS = frozenset({"uuid", "database", "table"})


@dataclass(frozen=True)
class ReplicaIdentity:
    """How a replicated table the restore creates gets its Keeper path. In the
    backup's own database it keeps the recorded one (the group a lost table
    left); elsewhere it takes the server's default path under a fresh UUID, or
    none when a Replicated database names it."""

    keep: bool
    defaults: tuple[str, str] | None = None


def replica_identity(
    client: Any, database: str, source_database: str, plans: list[TablePlan], state: TargetState
) -> ReplicaIdentity | None:
    """The identity the missing replicated tables are created with; None when
    none is created. Raises Refused when no independent one can be had."""
    creates = [p for p in plans if p.name in state.missing and _REPLICA_ARGS.search(p.ddl)]
    if not creates:
        return None
    macros = {
        str(r[0]): str(r[1])
        for r in client.query("SELECT macro, substitution FROM system.macros").result_rows
    }
    if database == source_database:
        for p in creates:
            path = _REPLICA_ARGS.search(p.ddl).group(2)  # type: ignore[union-attr]
            if "{uuid}" not in path and _live_replicas(client, path, macros):
                raise Refused(
                    f"{p.name}: its Keeper path {path!r} still has live replicas, and the "
                    "restored table would join them; create the table on this server "
                    "yourself (it fetches from them) and restore with --into-nonempty, "
                    "or restore into another database"
                )
        return ReplicaIdentity(keep=True)
    rows = client.query(
        "SELECT engine FROM system.databases WHERE name = {db:String}", parameters={"db": database}
    ).result_rows
    engine = str(rows[0][0]) if rows else _default_database_engine(client)
    if engine == "Replicated":
        return ReplicaIdentity(keep=False)
    defaults = {
        str(r[0]): str(r[1])
        for r in client.query(
            "SELECT name, value FROM system.server_settings "
            "WHERE name IN ('default_replica_path', 'default_replica_name')"
        ).result_rows
    }
    path = defaults.get("default_replica_path", "")
    name = defaults.get("default_replica_name", "")
    used = {m for m in _MACRO.findall(path + name) if m not in _TABLE_MACROS}
    lacking = sorted(used - set(macros)) if path and name else ["default_replica_path"]
    if lacking:
        raise Refused(
            f"{', '.join(p.name for p in creates)}: a replicated table restored into "
            f"{database!r} needs its own Keeper path, and the server's default "
            f"({path!r}, {name!r}) uses macros it does not define ({', '.join(lacking)}); "
            "define them, create the tables yourself first, or restore into the backup's "
            "own database"
        )
    return ReplicaIdentity(keep=False, defaults=(path, name))


def _default_database_engine(client: Any) -> str:
    rows = client.query(
        "SELECT value FROM system.settings WHERE name = 'default_database_engine'"
    ).result_rows
    return str(rows[0][0]) if rows else "Atomic"


def _live_replicas(client: Any, path: str, macros: dict[str, str]) -> int:
    """How many replicas the Keeper path still holds (0 when it is gone)."""
    resolved = _MACRO.sub(lambda m: macros.get(m.group(1), m.group(0)), path)
    try:
        rows = client.query(
            "SELECT count() FROM system.zookeeper WHERE path = {p:String}",
            parameters={"p": resolved.rstrip("/") + "/replicas"},
        ).result_rows
    except Exception as e:
        if "No node" in str(e) or "NO_NODE" in str(e):
            return 0
        raise Refused(f"could not read Keeper path {resolved!r}: {e}") from e
    return int(rows[0][0]) if rows else 0


def target_ddl(ddl: str, database: str, table: str, identity: ReplicaIdentity | None) -> str:
    """`retarget_ddl`, its replicated engine given the restore's identity."""
    out = retarget_ddl(ddl, database, table)
    m = _REPLICA_ARGS.search(out)
    if m is None or identity is None:
        return out
    if identity.keep and "{uuid}" not in m.group(2):
        return out
    if identity.keep or identity.defaults is not None:
        if identity.defaults is not None:
            path, name = identity.defaults
            sep = ", " if m.group(4) else ""
            out = f"{out[: m.start()]}{m.group(1)}'{path}', '{name}'{sep}{out[m.end() :]}"
        # `{uuid}` in a path needs the table's UUID written out: a fresh one.
        return out.replace(
            f"`{database}`.`{table}` ", f"`{database}`.`{table}` UUID '{uuid.uuid4()}' ", 1
        )
    return f"{out[: m.start()]}{m.group(1)}{out[m.end() :]}"


def staging_engine_ddl(create_query: str, staging: str) -> str | None:
    """The table's CREATE statement for `staging` with its replicated engine made
    local, or None when the engine is not replicated. A copy would register a
    second table at the same Keeper path (REPLICA_ALREADY_EXISTS); the staging
    table is filled and drained on this server only, and its parts attach to the
    replicated table, which replicates them."""
    if not _REPLICATED.search(create_query):
        return None
    head, n = _CREATE_HEAD.subn(f"CREATE TABLE {staging} ", create_query, 1)
    if n != 1:
        return None
    return _REPLICATED.sub(lambda m: f"{m.group(1)}(", head, 1)


def create_settings(ddl: str) -> dict[str, int]:
    """What a CREATE of a coded vector table (ClickHouse 26.8+) must send."""
    from server.core.schema.vector_codec_ddl import statement_settings

    return statement_settings(ddl)


def staging_ddl(client: Any, database: str, table: str, staging: str) -> tuple[str, dict[str, int]]:
    """The statement that creates `table`'s staging copy, and its settings (an
    `AS` copy carries the source's codecs). A Replicated database names each
    table's Keeper path itself, so a copy there is safe as it is."""
    row = client.query(
        "SELECT d.engine, t.create_table_query FROM system.tables AS t "
        "JOIN system.databases AS d ON d.name = t.database "
        "WHERE t.database = {db:String} AND t.name = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    source = str(row[0][1]) if row else ""
    if row and row[0][0] != "Replicated":
        local = staging_engine_ddl(source, staging)
        if local is not None:
            return local, create_settings(source)
    return f"CREATE TABLE {staging} AS `{database}`.`{table}`", create_settings(source)


def staging_prefix(run_id: str) -> str:
    return f"_restore_{run_id}_"


def drop_staging(client: Any, database: str, run_id: str) -> list[str]:
    """Drop the staging tables run `run_id` left in `database`; their names."""
    names = [
        str(r[0])
        for r in client.query(
            "SELECT name FROM system.tables "
            "WHERE database = {db:String} AND startsWith(name, {prefix:String})",
            parameters={"db": database, "prefix": staging_prefix(run_id)},
        ).result_rows
    ]
    for name in names:
        client.command(f"DROP TABLE IF EXISTS `{database}`.`{name}` SYNC")
    return names


def lock_table(database: str) -> str:
    return f"`default`.`_loom_restore_lock_{database}`"


def _lock_holder(client: Any, database: str) -> str:
    rows = client.query(
        "SELECT comment FROM system.tables WHERE database = 'default' AND name = {t:String}",
        parameters={"t": f"_loom_restore_lock_{database}"},
    ).result_rows
    return str(rows[0][0]) if rows else ""


def acquire_lock(client: Any, database: str, run_id: str) -> None:
    """Claim `database` for this run, waiting while another run holds it. A
    CREATE is atomic on the name within one `default` database: on Cloud or one
    server that is every run; on a self-managed cluster, every run through the
    same server. Raises Refused when the claim stays held past `_LOCK_WAIT_S`."""
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            client.command(
                f"CREATE TABLE {lock_table(database)} (run_id String) "
                f"ENGINE = MergeTree ORDER BY tuple() COMMENT '{run_id}'"
            )
            return
        except Exception as e:
            if "TABLE_ALREADY_EXISTS" not in str(e) and "Code: 57" not in str(e):
                raise
        if time.monotonic() >= deadline:
            holder = _lock_holder(client, database) or "?"
            raise Refused(
                f"run {holder} still holds the restore lock of {database!r}. A large "
                "restore can run longer: check it is gone (no process, no growing "
                f"_restore_{holder}_* tables) before --drop-staging {holder}"
            )
        time.sleep(1.0)


def release_lock(client: Any, database: str) -> None:
    with contextlib.suppress(Exception):
        client.command(f"DROP TABLE IF EXISTS {lock_table(database)} SYNC")


def execute(
    client: Any,
    database: str,
    plans: list[TablePlan],
    state: TargetState,
    run_id: str,
    identity: ReplicaIdentity | None = None,
) -> tuple[list[str], str | None]:
    """Create what is missing, then insert each file in order; stop at the
    first failure. Returns (tables inserted, error or None)."""
    done: list[str] = []
    try:
        if not state.database_exists:
            client.command(f"CREATE DATABASE IF NOT EXISTS `{database}`")
        for p in plans:
            if p.name in state.missing:
                ddl = target_ddl(p.ddl, database, p.name, identity)
                client.command(ddl, settings=create_settings(ddl) or None)
                print(f"+ created {database}.{p.name}")
    except Exception as e:  # noqa: BLE001 — reported as a failed create, nothing inserted yet
        return done, f"create failed: {e}"
    # Each file loads into a staging copy of its table, whose parts then attach
    # to the table: an attach fires no materialized view, so a restored row is
    # not copied again into a table the backup restores too (memory_versions),
    # and the live views keep running for the writes of a live database.
    for p in plans:
        if p.rows == 0:
            continue
        staging = f"`{database}`.`{staging_prefix(run_id)}{p.name}`"
        try:
            ddl, ddl_settings = staging_ddl(client, database, p.name, staging)
            client.command(ddl, settings=ddl_settings or None)
            # A merge in staging would collapse rows before the attach; the
            # rows are the same to a FINAL read, but the restored count would
            # no longer match the manifest's. Needs the SYSTEM STOP MERGES
            # grant; without it merges in staging may collapse rows and the
            # restored count can differ from the manifest's.
            with contextlib.suppress(Exception):
                client.command(f"SYSTEM STOP MERGES {staging}")
            with p.path.open("rb") as f:
                client.raw_insert(
                    table=staging, insert_block=f, fmt="JSONEachRow", settings=_INSERT_SETTINGS
                )
            print(f"… attaching {database}.{p.name}", flush=True)
            client.command(
                f"ALTER TABLE `{database}`.`{p.name}` ATTACH PARTITION ALL FROM {staging}"
            )
        except Exception as e:  # noqa: BLE001 — reported with the tables already restored
            return done, f"{p.name}: {e}"
        finally:
            # A connection that failed the load fails this too: keep the report.
            with contextlib.suppress(Exception):
                client.command(f"DROP TABLE IF EXISTS {staging} SYNC")
        done.append(p.name)
        print(f"✓ {database}.{p.name}: {p.rows} rows")
    return done, None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore a Loom backup")
    parser.add_argument("--in-dir", help="Directory containing the backup (with manifest.json)")
    parser.add_argument(
        "--tables", default=None, help="Comma-separated subset; default: all in manifest"
    )
    parser.add_argument(
        "--exclude", default=None, help="Comma-separated tables of the backup to leave out"
    )
    parser.add_argument(
        "--allow-view-split",
        action="store_true",
        help="Allow restoring one end of a materialized view without the other",
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
    parser.add_argument(
        "--drop-staging",
        metavar="RUN_ID",
        help="Drop the staging tables a crashed run left (its id is printed at its start)",
    )
    add_target_args(parser)
    return parser


def live_databases() -> set[str]:
    """`loom`, plus the database the server configuration names."""
    from server.settings import settings

    return {LIVE_DATABASE, settings.ch_database}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.drop_staging is not None:
        return _drop_staging(args)
    if not args.in_dir:
        parser.error("--in-dir is required")
    in_dir = Path(args.in_dir).resolve()
    try:
        target = resolve_target(args)
        if not _IDENT.match(target.database):
            raise Refused(f"database name {target.database!r} is not a plain identifier")
        manifest = load_manifest(in_dir)
        plans = verify_files(
            in_dir, select_tables(manifest, args.tables, args.skip_idempotency, args.exclude)
        )
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
            if not args.allow_view_split:
                check_view_split(plans, manifest["views"])
            check_refusals(
                target.database,
                live_databases(),
                plans,
                state,
                into_live=args.into_live,
                into_nonempty=args.into_nonempty,
            )
            replica_identity(
                client, target.database, str(manifest.get("source_database") or ""), plans, state
            )
        except Refused as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
        if args.dry_run:
            print("Dry run: nothing written.")
            return 0
        run_id = secrets.token_hex(4)
        print(
            f"Run {run_id}: staging tables {staging_prefix(run_id)}*; after a crash, "
            f"remove them with --drop-staging {run_id}",
            flush=True,
        )
        try:
            acquire_lock(client, target.database, run_id)
        except Refused as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
        try:
            # Under the lock: the target as any earlier run left it.
            try:
                state = inspect_target(client, target.database, plans, org)
            except Exception as e:  # noqa: BLE001 — a failed read-only inspection writes nothing
                print(f"✗ could not inspect {target.describe()}: {e}", file=sys.stderr)
                return 2
            check_refusals(
                target.database,
                live_databases(),
                plans,
                state,
                into_live=args.into_live,
                into_nonempty=args.into_nonempty,
            )
            identity = replica_identity(
                client, target.database, str(manifest.get("source_database") or ""), plans, state
            )
            done, error = execute(client, target.database, plans, state, run_id, identity)
        except Refused as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2
        finally:
            release_lock(client, target.database)
    finally:
        client.close()
    total = sum(p.rows for p in plans if p.name in done)
    if error:
        print(f"✗ {error}", file=sys.stderr)
        print(f"✗ partial restore: inserted {done or 'nothing'}; the rest was not", file=sys.stderr)
        return 1
    print(f"\nRestored {total} rows across {len(done)} tables into {target.database}")
    return 0


def _drop_staging(args: argparse.Namespace) -> int:
    if not _RUN_ID.match(args.drop_staging):
        print(f"✗ {args.drop_staging!r} is not a run id (8 hex digits)", file=sys.stderr)
        return 2
    try:
        target = resolve_target(args)
        if not _IDENT.match(target.database):
            raise Refused(f"database name {target.database!r} is not a plain identifier")
        client = target.client(use_database=False)
    except (TargetError, Refused) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    try:
        names = drop_staging(client, target.database, args.drop_staging)
        if _lock_holder(client, target.database) == args.drop_staging:
            release_lock(client, target.database)
            names.append(f"default._loom_restore_lock_{target.database}")
    finally:
        client.close()
    print(f"dropped {len(names)} staging table(s) of run {args.drop_staging}: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
