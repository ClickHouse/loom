"""Sort-key and partition-key changes a migration declares.

A sorting key or a partition key is CREATE-only: ClickHouse changes neither in
place (it only appends new columns to a sorting key). The new layout needs a
new table, the rows copied into it, and `EXCHANGE TABLES`. A copy at boot is
not acceptable, so a migration only declares the target:

    -- loom: rebuild-if-empty code_test_facets
    CREATE TABLE loom.code_test_facets ( ...every column... ) ... ORDER BY (...);

That `CREATE TABLE` is the one place the target DDL lives. The runner and the
operator tool (`scripts/ops/table_rebuild.py`) both read it from the file.

What the runner does with each declared table:

- its live sorting and partition key already match the target (the operator
  ran the tool first): nothing, and the file records;
- it is empty (a fresh install): a twin with the target DDL is exchanged in and
  the old, empty table dropped, which is instant;
- it holds rows: the file fails, naming the tool. A boot never copies rows.

The target must restate the live table's columns exactly (names, types,
defaults, codecs): the swap changes where rows sit, not what a serving build
reads or writes, so it stays additive. Both the runner and the tool check it
against the live table before any exchange.
"""

from __future__ import annotations

import contextlib
import json
import re
import signal
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.core.paths import MIGRATIONS_DIR
from server.core.schema.journal import sorted_migrations, split_statements

TWIN_SUFFIX = "_rebuild"
PRE_SUFFIX = "_pre_rebuild"  # the old table after the tool's swap, kept for rollback
TOOL = "scripts/ops/table_rebuild.py"

_DIRECTIVE_RE = re.compile(
    r"^--[ \t]*loom:[ \t]*rebuild-if-empty[ \t]+(\w+)[ \t]*(?:--.*)?$", re.MULTILINE
)
_TARGET_RE = re.compile(r"^\s*CREATE\s+TABLE\s+loom\.(\w+)\s*\(", re.IGNORECASE)
_ORDER_RE = re.compile(r"^\s*ORDER BY\s+(.+?)\s*$", re.MULTILINE)
_PARTITION_RE = re.compile(r"^\s*PARTITION BY\s+(.+?)\s*$", re.MULTILINE)
CONSISTENT = {"select_sequential_consistency": 1}


class RebuildBlocked(RuntimeError):
    """A declared table holds rows in the old layout."""


@dataclass(frozen=True)
class Layout:
    """What decides where a row sits and which rows a ReplacingMergeTree merges."""

    sorting_key: str
    partition_key: str


def declared_tables(sql: str) -> list[str]:
    """The tables a migration file declares with `-- loom: rebuild-if-empty <table>`."""
    return list(dict.fromkeys(_DIRECTIVE_RE.findall(sql)))


def target_table(stmt: str) -> str | None:
    """`x` for a bare `CREATE TABLE loom.x (` statement (no IF NOT EXISTS), else None."""
    m = _TARGET_RE.match(stmt)
    return m.group(1) if m else None


def _split_top(expr: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in expr:
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        cur += ch
    return [p.strip().replace("`", "") for p in [*out, cur] if p.strip()]


def _wrapped(e: str) -> bool:
    """`e` is one parenthesised group: `(a, b)`, not `(a) + (b)`."""
    depth = 0
    for i, ch in enumerate(e):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth == 0 and i < len(e) - 1:
            return False
    return e.startswith("(")


def _key(expr: str) -> str:
    """A key expression as `system.tables` prints it: `(a, b)` → `a, b`."""
    e = expr.strip()
    parts = _split_top(e[1:-1] if _wrapped(e) else e)
    return "" if parts in ([], ["tuple()"]) else ", ".join(parts)


def key_columns(key: str) -> list[str]:
    """The top-level expressions of a key as `system.tables` prints it:
    `org, cityHash64(a, b)` → `["org", "cityHash64(a, b)"]`."""
    return _split_top(key)


def layout_of(stmt: str) -> Layout:
    """The sorting and partition key a CREATE TABLE statement declares."""
    order = _ORDER_RE.search(stmt)
    part = _PARTITION_RE.search(stmt)
    return Layout(
        sorting_key=_key(order.group(1)) if order else "",
        partition_key=_key(part.group(1)) if part else "",
    )


def twin_statement(stmt: str, table: str, twin: str) -> str:
    """The target statement creating `loom.<twin>` instead of `loom.<table>`."""
    pat = rf"(CREATE\s+TABLE\s+loom\.){re.escape(table)}\b"
    out, n = re.subn(pat, rf"\g<1>{twin}", stmt, count=1)
    if n != 1:
        raise ValueError(f"not the target statement of loom.{table}")
    return out


def find_target(table: str, mig_dir: Path = MIGRATIONS_DIR) -> tuple[str, str]:
    """(file name, target statement) of the newest migration declaring `table`."""
    found: tuple[str, str] | None = None
    for path in sorted_migrations(mig_dir.glob("*.sql")):
        sql = path.read_text("utf-8")
        if table not in declared_tables(sql):
            continue
        for stmt in split_statements(sql):
            if target_table(stmt) == table:
                found = (path.name, stmt)
    if found is None:
        raise LookupError(f"no migration declares `-- loom: rebuild-if-empty {table}`")
    return found


def live_layout(ch: Any, table: str) -> Layout | None:
    rows = ch.query(
        "SELECT sorting_key, partition_key FROM system.tables "
        "WHERE database = currentDatabase() AND name = {t:String}",
        parameters={"t": table},
    ).result_rows
    return Layout(str(rows[0][0]), str(rows[0][1])) if rows else None


def copy_sql(src: str, dst: str, cols: Iterable[str], where: str = "") -> str:
    """`INSERT INTO loom.<dst> SELECT` the named columns of `loom.<src>`, every
    row or those matching `where` (which may carry query parameters)."""
    names = ", ".join(f"`{c}`" for c in cols)
    tail = f" WHERE {where}" if where else ""
    return f"INSERT INTO loom.{dst} ({names}) SELECT {names} FROM loom.{src}{tail}"


def row_count(ch: Any, table: str) -> int:
    return int(ch.query(f"SELECT count() FROM loom.{table}", settings=CONSISTENT).result_rows[0][0])


def columns(ch: Any, table: str) -> dict[str, tuple[Any, ...]]:
    """name -> (type, default kind, default expression, codec). Order is left
    out: a column added by ALTER sits last on an old deployment and in place on
    a fresh one, and every reader and writer names its columns."""
    rows = ch.query(
        "SELECT name, type, default_kind, default_expression, compression_codec "
        "FROM system.columns WHERE database = currentDatabase() AND table = {t:String} "
        "ORDER BY position",
        parameters={"t": table},
    ).result_rows
    return {str(r[0]): tuple(r[1:]) for r in rows}


def insertable_columns(ch: Any, table: str) -> list[str]:
    """The columns an INSERT may name: every one but MATERIALIZED and ALIAS,
    which the server computes and refuses to be given."""
    return [
        n for n, c in columns(ch, table).items() if c[1:2] not in (("MATERIALIZED",), ("ALIAS",))
    ]


def same_columns(ch: Any, table: str, twin: str) -> None:
    """Raise unless `twin` has exactly the columns of `table`."""
    have, want = columns(ch, table), columns(ch, twin)
    if have != want:
        diff = sorted(n for n in set(have) | set(want) if have.get(n) != want.get(n))
        raise RuntimeError(
            f"the target DDL of loom.{table} does not restate its columns (differs on {diff}); "
            "a rebuild moves rows, it never changes what a build reads or writes"
        )


def state(ch: Any, table: str, stmt: str) -> tuple[str, int]:
    """(`matches` | `missing` | `empty` | `blocked`, rows) for one declared table."""
    live = live_layout(ch, table)
    if live is None:
        return "missing", 0
    if live == layout_of(stmt):
        return "matches", 0
    n = row_count(ch, table)
    return ("blocked", n) if n else ("empty", 0)


def preflight(ch: Any, sql: str) -> dict[str, tuple[str, int]]:
    """The state of every table `sql` declares; raises RebuildBlocked naming
    every table that holds rows in the old layout."""
    tables = declared_tables(sql)
    targets = {t: s for s in split_statements(sql) if (t := target_table(s)) in tables}
    missing = [t for t in tables if t not in targets]
    if missing:
        raise ValueError(f"rebuild-if-empty without a `CREATE TABLE loom.<t>` target: {missing}")
    states = {t: state(ch, t, targets[t]) for t in tables}
    for t, (s, n) in states.items():
        print(f"    · loom.{t}: " + _STATE_TEXT[s].format(n=n))
    blocked = {t: n for t, (s, n) in states.items() if s == "blocked"}
    if blocked:
        raise RebuildBlocked(
            "these tables hold rows in the old layout: "
            + ", ".join(f"loom.{t} ({n} rows)" for t, n in blocked.items())
            + f". For each, run {TOOL} with `python -P -m scripts.ops.table_rebuild "
            "--table <t> --dry-run`, then again with `--yes` in place of `--dry-run`; "
            "then boot again"
        )
    return states


_STATE_TEXT = {
    "matches": "already has the target layout; nothing to do",
    "missing": "absent; created with the target layout",
    "empty": "empty; rebuilt in place with the target layout",
    "blocked": "holds {n} rows in the old layout",
}


def apply(ch: Any, table: str, stmt: str) -> None:
    """Bring one declared table to its target layout without copying rows."""
    kind, _ = state(ch, table, stmt)
    if kind == "matches":
        return
    if kind == "missing":
        ch.command(stmt)
        return
    if kind == "blocked":
        raise RebuildBlocked(
            f"loom.{table} holds rows; run {TOOL} --table {table} --dry-run, then --yes, first"
        )
    twin = table + TWIN_SUFFIX
    if live_layout(ch, twin) is not None:
        if row_count(ch, twin):
            raise RebuildBlocked(f"loom.{twin} holds rows: a {TOOL} run is in progress")
        ch.command(f"DROP TABLE loom.{twin} SYNC")
    ch.command(twin_statement(stmt, table, twin))
    same_columns(ch, table, twin)
    ch.command(f"EXCHANGE TABLES loom.{table} AND loom.{twin}")
    # A write that raced the exchange sits in the old table; move it over.
    if row_count(ch, twin):
        ch.command(copy_sql(twin, table, insertable_columns(ch, table)))
    ch.command(f"DROP TABLE loom.{twin} SYNC")


def targets_in(sql: str, stmts: Iterable[str]) -> dict[str, str]:
    """{statement: table} for the statements of `stmts` that are declared targets."""
    tables = set(declared_tables(sql))
    return {s: t for s in stmts if (t := target_table(s)) in tables}


# -- What the operator rebuild tools share ------------------------------------
# scripts/ops/table_rebuild.py (sort and partition keys) and
# scripts/ops/vector_codec_rebuild.py (vector layouts) both copy a table into a
# twin `<t>_rebuild`, swap it in, keep the old table as `<t>_pre_rebuild` for
# `--rollback` until `--drop-pre-rebuild`, and end every run with the next
# step and one `summary {json}` line.

OK, ABORTED, REFUSED, UNCONFIRMED, GATE_FAILED, TOUCHED, POSTCONDITION = 0, 2, 3, 4, 5, 6, 7
INTERRUPTED = 130


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%SZ", time.gmtime()), *a, flush=True)


class Interrupted(Exception):
    """SIGTERM, raised where the run was, so the cleanup and the report run."""


class Aborted(Exception):
    """The abort file appeared between two steps."""


def _on_sigterm(signum: int, frame: Any) -> None:
    raise Interrupted(f"signal {signum}")


@contextlib.contextmanager
def signals_deferred() -> Iterator[None]:
    """Hold SIGINT and SIGTERM over statements that must run as a pair; the
    signal is raised once the block is done."""
    seen: list[int] = []
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    for s in handlers:
        signal.signal(s, lambda n, f: seen.append(n))
    try:
        yield
    finally:
        for s, h in handlers.items():
            signal.signal(s, h)
    if seen:
        raise Interrupted(f"signal {seen[0]} (held over the exchange)")


def table_row(ch: Any, name: str) -> tuple[str, str, Any] | None:
    """(engine, uuid, metadata modification time) of `loom.<name>`, or None."""
    rows = ch.query(
        "SELECT engine, toString(uuid), metadata_modification_time FROM system.tables "
        "WHERE database = currentDatabase() AND name = {t:String}",
        parameters={"t": name},
        settings=CONSISTENT,
    ).result_rows
    return (str(rows[0][0]), str(rows[0][1]), rows[0][2]) if rows else None


def swap_in(ch: Any, table: str) -> None:
    """Exchange the twin in and rename the old table to the rollback copy, the
    two statements under held signals; then stop the copy's merges."""
    twin, pre = table + TWIN_SUFFIX, table + PRE_SUFFIX
    try:  # an insert still in the async queue at the swap fails with code 741
        ch.command(f"SYSTEM FLUSH ASYNC INSERT QUEUE loom.{table}")
    except Exception as e:  # noqa: BLE001 - a narrower window, not a precondition
        log("could not flush the async insert queue:", str(e)[:120])
    with signals_deferred():
        ch.command(f"EXCHANGE TABLES loom.{table} AND loom.{twin}")
        ch.command(f"RENAME TABLE loom.{twin} TO loom.{pre}")
    log(f"EXCHANGED: loom.{table} has the target layout; the old table is loom.{pre}")
    try:
        ch.command(f"SYSTEM STOP MERGES loom.{pre}")
    except Exception as e:  # noqa: BLE001 - merges on the rollback copy only cost a re-copy
        log("could not stop merges on the rollback copy:", str(e)[:120])


def guidance(table: str, run: str, live: bool | None, twin: bool, pre: bool | None) -> str:
    """The next step from what the three names hold. `live` / `pre`: None when
    the name is absent, else whether it holds the target layout; `run` is the
    tool's command line up to its flags. Pure."""
    tw, pr = f"loom.{table}{TWIN_SUFFIX}", f"loom.{table}{PRE_SUFFIX}"
    if live is None:
        return f"loom.{table} is absent: inspect {tw} and {pr} by hand before anything else"
    if live and twin and pre is None:
        return (
            f"the exchange ran but the rename did not: {tw} is the old table; run "
            f"`RENAME TABLE {tw} TO {pr}`, then `SYSTEM STOP MERGES {pr}`"
        )
    if live and pre is not None:
        return f"swapped: `{run} --rollback --yes` undoes it, `--drop-pre-rebuild --yes` keeps it"
    if live:
        return "on the target layout; nothing to do"
    if pre:
        return f"rolled back: `{run} --drop-pre-rebuild --yes` drops the new-layout copy {pr}"
    if pre is not None:
        return (
            f"{pr} holds an earlier layout and blocks a new run: `{run} --drop-pre-rebuild --yes`"
        )
    if twin:
        return f"not swapped; {tw} is left for inspection: `{run} --restart --yes` copies again"
    return f"not swapped and nothing left behind: `{run} --yes` runs it"


def run_reported(
    table: str,
    action: str,
    body: Callable[[], int],
    report: Callable[[], str],
    rows_copied: Callable[[], int],
    report_after: tuple[str, ...] = ("rebuild",),
) -> int:
    """Run a tool's action with SIGTERM raised as `Interrupted`, map an abort
    or an interrupt to its exit code, print the next step after anything but
    success, after a dry run and after a successful action in `report_after`
    (a rebuild's swap leaves the rollback copy to keep or drop), and end with
    one `summary {json}` line."""
    t0 = time.monotonic()
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    rc, next_step = 1, ""  # 1: an exception escaped; its traceback follows the summary
    try:
        rc = body()
    except Aborted as e:
        log("ABORTED:", e)
        rc = ABORTED
    except (KeyboardInterrupt, Interrupted) as e:
        log("INTERRUPTED:", str(e) or "keyboard interrupt")
        rc = INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)
        reports = action == "dry-run" or (rc == OK and action in report_after)
        if rc not in (OK, UNCONFIRMED) or reports:
            with contextlib.suppress(Exception):  # the report must not mask the exit code
                next_step = report()
                log("next:", next_step)
        summary = {
            "table": table,
            "action": action,
            "exit": rc,
            "rows_copied": rows_copied(),
            "elapsed_s": round(time.monotonic() - t0, 1),
            "next": next_step,
        }
        print("summary " + json.dumps(summary, sort_keys=True), flush=True)
    return rc


def print_target(ch: Any, table: str) -> None:
    """Which ClickHouse the run is about to change (never the password)."""
    from server.settings import settings

    log(
        f"ClickHouse {settings.ch_host}:{settings.ch_port} database {settings.ch_database} "
        f"user {settings.ch_user or '-'} password {'set' if settings.ch_password else 'unset'}"
    )
    (ver, host), *_ = ch.query("SELECT version(), hostName()", settings=CONSISTENT).result_rows
    log(f"  server {ver} on {host}")
    for total, active in ch.query(
        "SELECT total_replicas, active_replicas FROM system.replicas "
        "WHERE database = currentDatabase() AND table = {t:String}",
        parameters={"t": table},
        settings=CONSISTENT,
    ).result_rows:
        log(f"  replicas: {active} of {total} active")
