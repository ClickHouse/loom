"""A cell's org triggers: the store-side backstop that refuses a row for an org the cell
holds no tenancy row for, written by a session that did not run the tenancy check
(docs/architecture/cells.md, "Writers older than the check are refused by the stores
they write").

A session that ran the check marks itself with `loom.tenancy_checked = 'on'`: every
session of a process with LOOM_CELL set (a startup parameter of the pool and of
`pg.dedicated()`) and the migration session (`pg_migrate`). The triggers' WHEN skips a
marked session; `loom.tenancy_refuse_unheld()` (migration 011) judges the rest. The
guard on `loom.tenancy` itself, `tenancy_record` with `loom.tenancy_refuse_unmarked()`
(migration 012), refuses an unmarked session's tenancy row in every state but a first
cell still recording: a tenancy row makes an org held.

`ensure` creates the triggers on a fully prepared cell in `enforced` at a migrating
boot (`ensure_tenancy_guard` the guard), and `missing` names what a cell's triggers lack
for `loom doctor`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any

from server.core import cell, pg

log = logging.getLogger(__name__)

FUNCTION = "loom.tenancy_refuse_unheld"
INSERT_TRIGGER = "tenancy_insert"
UPDATE_TRIGGER = "tenancy_update"
ORG_TRIGGERS = (INSERT_TRIGGER, UPDATE_TRIGGER)
TENANCY_FUNCTION = "loom.tenancy_refuse_unmarked"
TENANCY_TRIGGER = "tenancy_record"
CELL_FIRST_TRIGGERS = ("cell_first_immutable", "cell_first_no_truncate")
# Org-keyed tables outside the guard: the cell's own tenancy rows, and the idempotency
# rows keyed by the caller's token org (a platform admin's replay row for its home org is
# neither refused nor placed).
EXCLUDED = frozenset({"tenancy", "idempotency", "idempotency_fences"})
# A table's DDL waits this long for its lock a try, LOCK_ATTEMPTS tries LOCK_RETRY_S
# apart: a boot never queues behind a long transaction (and every write of the table
# behind it; readers do not wait on its lock).
LOCK_TIMEOUT = "100ms"
LOCK_ATTEMPTS = 3  # not a setting: a boot's tries per table, then it serves without it
LOCK_RETRY_S = 1.0  # not a setting: the pause between them

# Ordinary and partitioned tables in schema loom with a live `org` column. A partition
# is left out: its parent is named unsupported, and guarding some partitions would leave
# one attached later unguarded until a boot.
_ORG_TABLES = """
SELECT c.relname, c.relkind::text AS relkind, pg_has_role(c.relowner, 'USAGE') AS owned
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = 'loom' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND a.attname = 'org' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname
"""

_TRIGGERS = """
SELECT c.relname, t.tgname, t.tgenabled::text AS enabled, t.tgtype::int AS tgtype,
       t.tgfoid = to_regprocedure(CASE t.tgname WHEN 'tenancy_record'
           THEN 'loom.tenancy_refuse_unmarked()' ELSE 'loom.tenancy_refuse_unheld()' END)
           AS calls_function,
       t.tgfoid::regprocedure::text AS function, pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'loom' AND NOT t.tgisinternal AND t.tgname = ANY($1::text[])
"""

_FUNCTION = """
SELECT p.provolatile::text AS volatile, p.prosecdef AS definer, p.prosrc AS body,
       coalesce(p.proconfig, '{}') AS config
FROM pg_proc p WHERE p.oid = to_regprocedure($1)
"""

# What a trigger's DDL (SHARE ROW EXCLUSIVE) waited behind on a table, read after the
# wait failed (pg_blocking_pids is empty by then): the locks held in a mode it conflicts
# with (every mode but the two readers take), and the requests queued ahead of it in
# one, whose own blocker heads the queue.
_HOLDERS = """
SELECT DISTINCT l.pid, l.granted, a.backend_type, a.state, a.application_name,
       a.xact_start, a.wait_event
FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.relation = $1::regclass AND l.pid <> pg_backend_pid()
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND l.mode NOT IN ('AccessShareLock', 'RowShareLock')
"""

# pg_trigger.tgtype bits (src/include/catalog/pg_trigger.h).
_ROW, _BEFORE, _INSERT, _UPDATE, _INSTEAD = 1, 2, 4, 16, 64


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _when(event: str) -> str:
    unmarked = f"current_setting('{pg.TENANCY_MARKER}', true) IS DISTINCT FROM 'on'"
    if event == "UPDATE":
        # Loom's own updates never change a row's org; an update of an org's existing
        # rows is not refused.
        return f"({unmarked} AND OLD.org IS DISTINCT FROM NEW.org)"
    return f"({unmarked})"


_ON_TABLE = re.compile(r" ON \S+ FOR EACH ROW ")


def definition_shape(definition: str) -> str:
    """A trigger's definition as `pg_get_triggerdef` prints it, its table left out: its
    timing, events (with any column list), level, WHEN and function."""
    return _ON_TABLE.sub(" ON - FOR EACH ROW ", definition, count=1)


async def expected_definitions(conn: Any, names: tuple[str, ...]) -> dict[str, str]:
    """Each named trigger's definition, as this server prints the one `_create` writes,
    its table left out: read from triggers created on a temporary table in a transaction
    rolled back."""
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("CREATE TEMP TABLE loom_when_probe (org text)")
        out = {}
        for name in names:
            await conn.execute(_create("loom_when_probe", name))
            definition = await conn.fetchval(
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgrelid = 'loom_when_probe'::regclass AND tgname = $1",
                name,
            )
            out[name] = definition_shape(definition)
        return out
    finally:
        await tx.rollback()


def expected_functions(function: str = FUNCTION) -> list[tuple[str, list[str]]]:
    """`function`'s body and settings as each migration file that defines it writes
    them: a function changes only through a numbered file (rolling-upgrades rule 47), so
    any other body is a hand edit. An earlier file's body counts too: a cell this build has
    not migrated yet keeps it."""
    from server.core.schema import pg_migrate

    pattern = _function_sql(function)
    out = []
    for f in pg_migrate.migration_files():
        found = pattern.search(f.read_text())
        if found is None:  # a file that does not define it, or only alters or drops it
            continue
        out.append((found["body"], [f"search_path={found['path'].strip()}"]))
    if not out:
        raise RuntimeError(f"no migration defines {function}()")
    return out


def _function_sql(function: str) -> re.Pattern[str]:
    """A function's definition in a migration file: its search_path and $$-quoted body."""
    return re.compile(
        rf"CREATE OR REPLACE FUNCTION {re.escape(function)}\(\)[^$]*?"
        r"SET search_path = (?P<path>[^\n]+)\s+AS \$\$(?P<body>.*?)\$\$",
        re.S,
    )


def _create(qualified: str, name: str) -> str:
    """The CREATE TRIGGER for `name` on the table `qualified` names."""
    if name == TENANCY_TRIGGER:
        return (
            f"CREATE TRIGGER {name} AFTER INSERT OR UPDATE OF org ON {qualified} FOR EACH ROW "
            f"WHEN {_when('INSERT')} EXECUTE FUNCTION {TENANCY_FUNCTION}()"
        )
    event = "INSERT" if name == INSERT_TRIGGER else "UPDATE"
    return (
        f"CREATE TRIGGER {name} AFTER {event} ON {qualified} FOR EACH ROW "
        f"WHEN {_when(event)} EXECUTE FUNCTION {FUNCTION}()"
    )


def _guarded(rows: list[Any]) -> list[Any]:
    """The org tables the triggers guard: ordinary ones, but the exclusions."""
    return [r for r in rows if r["relkind"] == "r" and r["relname"] not in EXCLUDED]


async def _triggers(conn: Any, names: tuple[str, ...]) -> dict[tuple[str, str], Any]:
    return {(r["relname"], r["tgname"]): r for r in await conn.fetch(_TRIGGERS, list(names))}


async def ensure(conn: Any) -> None:
    """Create a fully prepared `enforced` cell's absent org triggers and enable each one
    left otherwise than `O`; in any state but `open`, set each `loom.cell_first` trigger
    not at `A` to ENABLE ALWAYS. Never drops a trigger. A table's DDL runs in one
    transaction that waits LOCK_TIMEOUT for its lock a try, LOCK_ATTEMPTS tries; a table
    whose lock it cannot take is logged with the holders of its locks, one the role
    cannot alter is logged as such, and either is skipped (the boot serves). A failed
    read of the state or the catalog raises. Runs on the migration session, which holds
    the migration lock."""
    state = await cell.read_state(conn)
    if state == "open":
        return
    # A table's statements, in one transaction: one lock wait, and both triggers or neither.
    statements: dict[str, list[str]] = {}
    first = await _triggers(conn, CELL_FIRST_TRIGGERS)
    for name in CELL_FIRST_TRIGGERS:
        row = first.get(("cell_first", name))
        if row is not None and row["enabled"] != "A":
            statements.setdefault("cell_first", []).append(
                f"ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER {name}"
            )
    if state == "enforced":
        present = await _triggers(conn, ORG_TRIGGERS)
        tables = await conn.fetch(_ORG_TABLES)
        for r in tables:
            if r["relkind"] == "p":
                # No trigger guards it: named here as missing() names it.
                log.warning(
                    "cell triggers: loom.%s is partitioned: unsupported, unguarded", r["relname"]
                )
        for table in [r["relname"] for r in _guarded(tables)]:
            for name in ORG_TRIGGERS:
                row = present.get((table, name))
                if row is None:
                    statements.setdefault(table, []).append(_create(f"loom.{_ident(table)}", name))
                elif row["enabled"] != "O":
                    statements.setdefault(table, []).append(
                        f"ALTER TABLE loom.{_ident(table)} ENABLE TRIGGER {name}"
                    )
    for table, sqls in statements.items():
        await _run_ddl(conn, table, sqls)
    if state == "enforced":
        await ensure_tenancy_guard(conn)


async def ensure_tenancy_guard(conn: Any) -> None:
    """Create `tenancy_record` on `loom.tenancy` when it is absent, and enable it when it
    is left otherwise than `O`, as `ensure` does an org trigger (its own path: `_guarded`
    leaves `loom.tenancy` out). Nothing is done before migration 012 has created its
    function; `missing` names the trigger absent then."""
    if not await conn.fetchval("SELECT to_regprocedure($1) IS NOT NULL", f"{TENANCY_FUNCTION}()"):
        return
    row = (await _triggers(conn, (TENANCY_TRIGGER,))).get(("tenancy", TENANCY_TRIGGER))
    if row is None:
        await _run_ddl(conn, "tenancy", [_create("loom.tenancy", TENANCY_TRIGGER)])
    elif row["enabled"] != "O":
        await _run_ddl(
            conn, "tenancy", [f"ALTER TABLE loom.tenancy ENABLE TRIGGER {TENANCY_TRIGGER}"]
        )


async def _run_ddl(conn: Any, table: str, sqls: list[str]) -> None:
    """A table's DDL in one transaction, LOCK_ATTEMPTS short tries. The boot serves past
    the two causes cells.md names (its lock not taken, a table this role cannot alter);
    any other error fails it, as a failed read of the state does."""
    import asyncpg

    for attempt in range(LOCK_ATTEMPTS):
        try:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                for sql in sqls:
                    await conn.execute(sql)
            return
        except asyncpg.exceptions.InsufficientPrivilegeError as e:
            log.warning(
                "cell triggers: not applied on loom.%s (%s): this role cannot alter it", table, e
            )
            return
        except asyncpg.exceptions.LockNotAvailableError as e:
            # Short tries outlast a short transaction; an autovacuum is left to finish.
            if attempt < LOCK_ATTEMPTS - 1:
                await asyncio.sleep(LOCK_RETRY_S)
                continue
            log.warning(
                "cell triggers: not applied on loom.%s (%s): %s; %s",
                table,
                e,
                "; ".join(sqls),
                await _holders(conn, table),
            )


async def _holders(conn: Any, table: str) -> str:
    """The sessions a blocked DDL waited behind, never their query text (a literal in it
    may be memory content)."""
    try:
        rows = await conn.fetch(_HOLDERS, f"loom.{_ident(table)}")
    except Exception as e:  # noqa: BLE001 — naming the holders is best effort
        log.warning("cell triggers: the lock holders of loom.%s not read: %s", table, e)
        return "lock holders not read"
    return (
        "; ".join(
            f"pid {r['pid']} ({'holds' if r['granted'] else 'waiting'}, {r['backend_type']}, "
            f"{r['state']}, "
            f"{r['application_name'] or 'no name'}, "
            f"transaction since {r['xact_start']}, waiting on {r['wait_event'] or 'nothing'})"
            for r in rows
        )
        or "no lock holder found"
    )


@dataclasses.dataclass(frozen=True)
class Defect:
    """What `missing` names: `kind` is `absent` for an absent org trigger (normal before a
    cell's first migrating boot, so doctor names it only on an `enforced` cell)."""

    kind: str
    text: str


async def missing(conn: Any) -> list[Defect]:
    """What this cell's triggers and function lack, read with the migrating role."""
    found: list[Defect] = []
    tables = await conn.fetch(_ORG_TABLES)
    guarded = _guarded(tables)
    for r in tables:
        if r["relkind"] == "p":
            found.append(Defect("partitioned", f"loom.{r['relname']} is partitioned: unsupported"))
    present = await _triggers(conn, (*ORG_TRIGGERS, TENANCY_TRIGGER))
    fn = await conn.fetchrow(_FUNCTION, f"{FUNCTION}()")
    # A cell not migrated to 012 yet lacks the guard's function: absent, as its trigger.
    tenancy_fn = await conn.fetchrow(_FUNCTION, f"{TENANCY_FUNCTION}()")
    # Compared whole, as this server prints them; a trigger whose function is absent has
    # nothing to compare with. A role that may not create a temporary table cannot
    # compare: named, with every other defect kept.
    comparable = (ORG_TRIGGERS if fn is not None else ()) + (
        (TENANCY_TRIGGER,) if tenancy_fn is not None else ()
    )
    expected = None
    if comparable:
        try:
            expected = await expected_definitions(conn, comparable)
        except Exception as e:  # noqa: BLE001 — the other checks stand without the compare
            found.append(
                Defect("definition", f"trigger definitions not compared: {type(e).__name__}")
            )
    on_tenancy = [r for r in tables if r["relname"] == "tenancy"]
    for r in [*guarded, *on_tenancy]:
        table = r["relname"]
        if not r["owned"]:
            found.append(Defect("owner", f"loom.{table}: this role cannot alter it"))
        for name in (TENANCY_TRIGGER,) if table == "tenancy" else ORG_TRIGGERS:
            row = present.get((table, name))
            if row is None:
                found.append(Defect("absent", f"loom.{table}: no {name} trigger"))
                continue
            event = {
                INSERT_TRIGGER: _INSERT,
                UPDATE_TRIGGER: _UPDATE,
                TENANCY_TRIGGER: _INSERT | _UPDATE,
            }[name]
            tgtype = row["tgtype"]
            if row["enabled"] != "O":
                found.append(
                    Defect("enabled", f"loom.{table}: {name} is {row['enabled']!r}, not 'O'")
                )
            if (
                expected is not None
                and name in expected
                and definition_shape(row["definition"]) != expected[name]
            ):
                found.append(
                    Defect(
                        "definition",
                        f"loom.{table}: {name} has another definition "
                        "(its WHEN, columns or events)",
                    )
                )
            if not row["calls_function"]:
                found.append(Defect("function", f"loom.{table}: {name} calls {row['function']}"))
            if not (tgtype & _ROW) or tgtype & (_BEFORE | _INSTEAD) or tgtype & event != event:
                found.append(
                    Defect("shape", f"loom.{table}: {name} is not AFTER, row-level, on its event")
                )
    found += _function_defects(FUNCTION, fn, absent="function")
    found += _function_defects(TENANCY_FUNCTION, tenancy_fn, absent="absent")
    first = await _triggers(conn, CELL_FIRST_TRIGGERS)
    for name in CELL_FIRST_TRIGGERS:
        row = first.get(("cell_first", name))
        if row is None:
            found.append(Defect("cell_first", f"loom.cell_first: no {name} trigger"))
        elif row["enabled"] != "A":
            found.append(
                Defect("cell_first", f"loom.cell_first: {name} is {row['enabled']!r}, not 'A'")
            )
    return found


def _function_defects(function: str, fn: Any, *, absent: str) -> list[Defect]:
    """What a trigger function lacks: it exists, is VOLATILE, not SECURITY DEFINER, and
    has a body and settings a migration file writes."""
    if fn is None:
        return [Defect(absent, f"{function}() is absent")]
    found = []
    if fn["volatile"] != "v":
        found.append(Defect("function", f"{function}() is not VOLATILE"))
    if fn["definer"]:
        found.append(Defect("function", f"{function}() is SECURITY DEFINER"))
    if (fn["body"], list(fn["config"])) not in expected_functions(function):
        found.append(Defect("function", f"{function}() has a body or settings no migration writes"))
    return found
