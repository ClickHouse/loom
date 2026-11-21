"""The first attach's reads and writes on the cell's Postgres (docs/architecture/cells.md,
"Attaching a cell"): the session checks, the one-run lock, the catalog checks attach and
`loom doctor` share (the roles that can make an org held or lower the mode, the settings
and subscriptions that pass an unchecked writer), the trigger pass, the mode commit, its
durable read and the wait for the transactions that could have read `recording`.

Every check returns its refusals as `cell_ch.Refusal` (subject, reason), as the org scan
and the ClickHouse writer check do, so attach and doctor print the same names."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from server.cli.cell_ch import Refusal, Refused
from server.core import cell, cell_triggers, pg
from server.core.cell import ATTACH_LOCK_KEY

if TYPE_CHECKING:
    from server.cli.cell_control import Owner

_sleep = asyncio.sleep  # the seam the wait tests replace

# The pause between polls of a wait.
POLL_S = 1.0  # not a setting: a poll interval
# The least lock wait a try gets, however little of --wait is left.
TRY_FLOOR_MS = 100  # not a setting: one try's floor
# The migration lock's wait a trigger try (a boot holds it for its migrations), in ms; a
# try never waits longer than --wait has left.
_MIGRATION_LOCK_MS = 10_000  # not a setting: one try's bound
# A trigger try yields quickly to active writers.
_TRIGGER_LOCK_MS = 100  # not a setting: one try's bound
_TRIGGER_TRY_MARGIN_S = 8.0  # not a setting: catalog, DDL and standby allowance
# The mode commit's lock wait a try, in ms, and its statement bound's margin above it.
_MODE_LOCK_MS = 2_000  # not a setting: one try's bound
_MODE_STATEMENT_MARGIN_MS = 3_000  # not a setting: a margin over the lock wait
# COMMIT's standby wait is outside Postgres's statement timeout.
_MODE_STANDBY_S = 5.0  # not a setting: one try's standby allowance
# The server ends the lock session soon after its client is gone, releasing the lock.
_KEEPALIVE = {"tcp_keepalives_idle": "30", "tcp_keepalives_interval": "10"}


class Wait:
    """`--wait`: the wall-clock seconds attach's bounded waits share. Each wait runs inside
    `counting()`, which charges its elapsed time, lock tries included; time outside every
    wait (a scan) is never charged."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = float(seconds)
        self.spent = 0.0
        self._clock = clock
        self._since: float | None = None

    def used(self) -> float:
        """The seconds charged so far, the wait running now included."""
        running = 0.0 if self._since is None else self._clock() - self._since
        return self.spent + running

    def left(self) -> float:
        return max(0.0, self.seconds - self.used())

    @contextlib.contextmanager
    def counting(self) -> Iterator[None]:
        """Charge the block's wall time (a block inside another is charged once)."""
        if self._since is not None:
            yield
            return
        self._since = self._clock()
        try:
            yield
        finally:
            self.spent += self._clock() - self._since
            self._since = None

    def bound_ms(self, most_ms: int) -> int:
        """A lock timeout for one try: `most_ms`, or what is left when that is less, and
        never under `TRY_FLOOR_MS`: every try runs once, so `--wait 0` is one try of each
        wait with no retry, and only after a try is the wait judged spent."""
        return max(TRY_FLOOR_MS, min(most_ms, int(self.left() * 1000)))

    async def pause(self, seconds: float = POLL_S) -> bool:
        """Wait `seconds` (at most what is left); False once none is left."""
        step = min(seconds, self.left())
        if step <= 0:
            return False
        with self.counting():
            await _sleep(step)
        return True


@dataclass
class _Interrupted:
    error: BaseException


async def _work_result[T](work: Callable[[], Awaitable[T]]) -> T | _Interrupted:
    try:
        return await work()
    except (KeyboardInterrupt, SystemExit) as error:
        # These exceptions otherwise stop asyncio's loop before the owner can close.
        return _Interrupted(error)


async def supervise[T](conn: Any, work: Coroutine[Any, Any, T], *, end: float) -> T:
    """Bound owned work, reserving cleanup inside its original phase deadline.

    A failed borrowed transaction must remain alive until public close sends its
    cancellation. Closing the transport first can leave its backend locks held.
    """
    task = asyncio.create_task(_work_result(lambda: work))
    reserve = min(1.0, max(0.0, end - time.monotonic()) / 4)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, end - reserve - time.monotonic()))
        if not done:
            raise TimeoutError("attach Postgres phase deadline expired")
        result = task.result()
        if isinstance(result, _Interrupted):
            raise result.error
        return result
    except BaseException:
        try:
            await pg.close_owned(conn, end=end)
        except BaseException:  # noqa: BLE001 — preserve the work failure or cancellation.
            pass
        if not task.done():
            task.cancel()
        task.add_done_callback(pg._retrieve_task)
        try:
            await asyncio.wait({task}, timeout=max(0.0, end - time.monotonic()))
        except BaseException:  # noqa: BLE001 — repeated cancellation preserves the first failure.
            try:
                pg._terminate(conn)
            except BaseException:  # noqa: BLE001 — abort cannot replace the work failure.
                pass
        raise


def _split(key: int) -> str:
    """The `pg_locks` columns that hold a bigint advisory key, in this database."""
    return (
        f"l.locktype = 'advisory' AND l.classid = ({key} >> 32)::oid "
        f"AND l.objid = ({key} & 4294967295)::oid AND l.objsubid = 1 "
        "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )


# Who holds an advisory lock (`where` from `_split`), never a query's text.
_ADVISORY_HOLDER = """
SELECT l.pid, a.application_name, a.backend_start
FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid
WHERE {where} AND l.granted
"""

_LOCK_HOLDER = f"""
SELECT l.pid, a.application_name, a.backend_start, a.client_addr::text AS client
FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid
WHERE {_split(ATTACH_LOCK_KEY)} AND l.granted
"""

_IDENTITY = (
    "SELECT pg_backend_pid() AS pid, backend_start FROM pg_stat_activity "
    "WHERE pid = pg_backend_pid()"
)


async def direct_refusals(conn: Any, what: str) -> list[Refusal]:
    """Whether `conn` is one direct, marked session: the same backend across two
    transactions, and the tenancy marker kept through `RESET ALL` (a pooler that strips
    startup parameters drops it). Behind a transaction pooler a session-level lock would
    outlive the CLI on the pooler's server connection, and attach's tenancy rows would be
    refused as an unchecked writer's."""
    identities = []
    for _ in range(2):
        tx = conn.transaction()
        await tx.start()
        identities.append(await conn.fetchrow(_IDENTITY))
        await tx.commit()
    first, second = identities
    out = []
    if first is None or second is None or tuple(first) != tuple(second):
        out.append(
            Refusal(
                "postgres session",
                f"{what}'s session moved between backends: point LOOM_PG_DSN straight at "
                "Postgres, never through a pooler",
            )
        )
    await conn.execute("RESET ALL")  # a startup value survives it, a SET does not
    if await conn.fetchval("SELECT current_setting($1, true)", pg.TENANCY_MARKER) != "on":
        out.append(
            Refusal(
                "postgres session",
                f"{what}'s session loses {pg.TENANCY_MARKER}: run it with LOOM_CELL set, "
                "LOOM_PG_DSN straight at Postgres",
            )
        )
    return out


_HELD = f"""
SELECT EXISTS (SELECT 1 FROM pg_locks l
               WHERE {_split(ATTACH_LOCK_KEY)} AND l.granted AND l.pid = pg_backend_pid())
"""


async def lock_holder(conn: Any) -> str:
    """Who holds the attach lock ('' when no one), never a query's text."""
    rows = await conn.fetch(_LOCK_HOLDER)
    return "; ".join(
        f"pid {r['pid']} ({r['application_name'] or 'no name'}, from {r['client'] or 'local'}, "
        f"since {r['backend_start']})"
        for r in rows
    )


async def take_lock(conn: Any, what: str) -> None:
    """Take the attach lock on `conn` for the session's life, confirmed held by this
    backend; Refused naming its holder when another run holds it."""
    for name, value in _KEEPALIVE.items():
        await conn.execute(f"SET {name} = {value}")
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", ATTACH_LOCK_KEY):
        holder = await lock_holder(conn) or "a session that just ended"
        raise Refused(
            [
                Refusal(
                    "attach lock",
                    f"held by {holder}: an attach or a prepare is running; {what} "
                    "runs one at a time",
                )
            ]
        )
    if not await lock_held(conn):
        raise Refused(
            [
                Refusal(
                    "attach lock",
                    "taken but not held by this session's backend: a pooler between "
                    "this command and Postgres; point LOOM_PG_DSN straight at it",
                )
            ]
        )


async def lock_held(conn: Any) -> bool:
    """Whether `conn`'s backend holds the attach lock."""
    return bool(await conn.fetchval(_HELD))


async def encoding_refusals(conn: Any) -> list[Refusal]:
    """A cell's database is UTF8: its `text` then holds no NUL or invalid UTF-8, so every
    tenancy row within the length limit is an org a request can name."""
    encoding = await conn.fetchval(
        "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()"
    )
    if encoding == "UTF8":
        return []
    return [
        Refusal(
            "postgres database",
            f"its encoding is {encoding}, not UTF8: a cell's database is UTF8 "
            "(recreate it with ENCODING 'UTF8' and restore into it)",
        )
    ]


# -- who can make an org held, or lower the mode --------------------------------------

_SYSTEM_SCHEMA = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp\\_%'"
)

# Every right a login role may hold that lets it make an org held, lower the mode, or run
# its own SQL as the servers on their marked sessions: each row a role and why.
# Membership can supply a right through SET ROLE or an ADMIN regrant without INHERIT.
_RIGHTS = f"""
WITH owned AS (
    SELECT c.relowner AS owner, 'owns loom.' || c.relname AS why
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'loom' AND c.relkind IN ('r', 'p')
    UNION ALL
    SELECT p.proowner, 'owns loom.' || p.proname || '()' FROM pg_proc p
    WHERE p.oid IN (to_regprocedure('loom.tenancy_refuse_unheld()'),
                    to_regprocedure('loom.tenancy_refuse_unmarked()'),
                    to_regprocedure('loom.cell_first_immutable()'))
    UNION ALL
    SELECT d.datdba, 'owns the database' FROM pg_database d
    WHERE d.datname = current_database()
    UNION ALL
    SELECT n.nspowner, 'owns schema ' || n.nspname FROM pg_namespace n WHERE {_SYSTEM_SCHEMA}
),
login AS (SELECT oid, rolname FROM pg_roles WHERE rolcanlogin)
SELECT DISTINCT l.rolname AS role, w.why FROM login l
JOIN pg_roles r ON pg_has_role(l.oid, r.oid, 'MEMBER'), LATERAL (
    SELECT 'may insert into loom.tenancy'
    WHERE has_any_column_privilege(r.oid, 'loom.tenancy', 'INSERT')
    UNION ALL SELECT 'may update loom.tenancy (org)'
    WHERE has_column_privilege(r.oid, 'loom.tenancy', 'org', 'UPDATE')
    UNION ALL SELECT 'may update loom.tenancy_mode (mode)'
    WHERE has_column_privilege(r.oid, 'loom.tenancy_mode', 'mode', 'UPDATE')
    UNION ALL SELECT 'may insert into loom.tenancy_mode'
    WHERE has_any_column_privilege(r.oid, 'loom.tenancy_mode', 'INSERT')
    UNION ALL SELECT 'holds TRIGGER on loom.' || c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'loom' AND c.relkind IN ('r', 'p')
      AND has_table_privilege(r.oid, c.oid, 'TRIGGER')
    UNION ALL SELECT 'may create in the database'
    WHERE has_database_privilege(r.oid, current_database(), 'CREATE')
    UNION ALL SELECT 'may create in schema ' || n.nspname FROM pg_namespace n
    WHERE {_SYSTEM_SCHEMA} AND has_schema_privilege(r.oid, n.oid, 'CREATE')
    UNION ALL SELECT 'may set session_replication_role'
    WHERE has_parameter_privilege(r.oid, 'session_replication_role', 'SET')
       OR has_parameter_privilege(r.oid, 'session_replication_role', 'ALTER SYSTEM')
    UNION ALL SELECT 'may ALTER SYSTEM SET {pg.TENANCY_MARKER}'
    WHERE has_parameter_privilege(r.oid, '{pg.TENANCY_MARKER}', 'ALTER SYSTEM')
    UNION ALL SELECT 'is a member of ' || g.rolname FROM pg_roles g
    WHERE g.rolname IN ('pg_write_server_files', 'pg_execute_server_program')
      AND pg_has_role(r.oid, g.oid, 'MEMBER')
    UNION ALL SELECT o.why || CASE WHEN o.owner = l.oid THEN ''
                                   ELSE ' (as a member of ' || pg_get_userbyid(o.owner) || ')'
                              END
    FROM owned o WHERE pg_has_role(l.oid, o.owner, 'MEMBER')
) w(why)
ORDER BY 1, 2
"""

# The roles attach counts as its own: superusers, a login role that itself owns
# loom.tenancy, loom.tenancy_mode or a guarded table (it can drop or disable the
# triggers, or lower the mode, whatever else it holds), and, for attach only, the role it
# runs as ($2): the operator runs attach, while doctor may run as a CI or monitoring role.
_OWN_ROLES = """
SELECT rolname FROM pg_roles WHERE rolcanlogin AND (rolsuper OR oid IN (
    SELECT c.relowner FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'loom' AND c.relkind IN ('r', 'p')
      AND (c.relname IN ('tenancy', 'tenancy_mode') OR c.relname = ANY($1::text[]))
)) OR ($2 AND rolname = current_user)
ORDER BY 1
"""


@dataclass(frozen=True)
class Holders:
    """The login roles holding a right `_RIGHTS` counts: the ones attach counts as its own
    (`own`, named in its output) and every other with its reasons."""

    own: list[str]
    others: dict[str, list[str]]


async def privilege_holders(conn: Any, *, exempt_self: bool) -> Holders:
    """The holders of a right `_RIGHTS` counts; `exempt_self` counts the role this session
    runs as among the own roles (attach's, not doctor's)."""
    guarded = [t for t in await cell_triggers.planned_tables(conn) if t != "tenancy"]
    own = [str(r["rolname"]) for r in await conn.fetch(_OWN_ROLES, guarded, exempt_self)]
    others: dict[str, list[str]] = {}
    for r in await conn.fetch(_RIGHTS):
        if r["role"] not in own:
            others.setdefault(str(r["role"]), []).append(str(r["why"]))
    return Holders(own, others)


def privilege_refusals(holders: Holders, allowed: Iterable[str]) -> list[Refusal]:
    allow = set(allowed)
    return [
        Refusal(
            f"postgres role {role}",
            f"{'; '.join(why)}: it can make an org held or lower the mode outside attach; "
            "revoke it, or name the role with --pg-allow-role",
        )
        for role, why in sorted(holders.others.items())
        if role not in allow
    ]


# -- what passes an unchecked writer through the triggers -----------------------------

_ROLE_SETTINGS = f"""
SELECT coalesce(r.rolname, 'every role') AS role, coalesce(d.datname, 'every database') AS db,
       cfg
FROM pg_db_role_setting s
LEFT JOIN pg_roles r ON r.oid = s.setrole
LEFT JOIN pg_database d ON d.oid = s.setdatabase,
LATERAL unnest(s.setconfig) cfg
WHERE s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))
  AND lower(split_part(cfg, '=', 1)) IN ('{pg.TENANCY_MARKER}', 'session_replication_role')
ORDER BY 1, 2, 3
"""

_SUBSCRIPTIONS = """
SELECT subname, subenabled FROM pg_subscription
WHERE subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY 1
"""


async def unmarked_refusals(conn: Any, session_passes: bool) -> list[Refusal]:
    """What lets a session that sends no marker pass the triggers: the session read
    (`session_passes`, doctor's unmarked session), an `ALTER ROLE … SET` or
    `ALTER DATABASE … SET` of the marker or `session_replication_role` for this database or
    every one, and any logical-replication subscription into this database (its apply
    worker runs as `replica`, firing no origin trigger)."""
    out = []
    if session_passes:
        out.append(
            Refusal(
                "postgres unmarked session",
                "a session that sends no marker is marked, or runs as replica (a role, "
                "database, ALTER SYSTEM or options setting): every unchecked writer passes",
            )
        )
    for r in await conn.fetch(_ROLE_SETTINGS):
        out.append(
            Refusal(
                f"postgres setting for {r['role']} in {r['db']}",
                f"{r['cfg']}: an unchecked writer under it passes the triggers; reset it",
            )
        )
    for r in await conn.fetch(_SUBSCRIPTIONS):
        state = "enabled" if r["subenabled"] else "disabled"
        out.append(
            Refusal(
                f"postgres subscription {r['subname']}",
                f"{state}: its apply worker fires no origin trigger; drop it",
            )
        )
    return out


async def session_passes() -> bool:
    """Whether a session that sends no marker passes the triggers all the same: marked,
    or in the replica role that fires no origin trigger."""
    async with pg.dedicated(statement_timeout_ms=5000, marked=False) as conn:
        value = await conn.fetchval("SELECT current_setting($1, true)", pg.TENANCY_MARKER)
        role = await conn.fetchval("SELECT current_setting('session_replication_role')")
    return bool(value == "on" or role == "replica")


# -- attach's own role ------------------------------------------------------------------

_OWN_GRANTS = """
SELECT need, held FROM (VALUES
  ('effective privileges of pg_read_all_stats',
   pg_has_role(current_user, 'pg_read_all_stats', 'USAGE')),
  ('USAGE on schema loom', has_schema_privilege('loom', 'USAGE')),
  ('SELECT on loom.cell_first', has_table_privilege('loom.cell_first', 'SELECT')),
  ('SELECT on loom.tenancy_mode', has_table_privilege('loom.tenancy_mode', 'SELECT')),
  ('UPDATE on loom.tenancy_mode', has_table_privilege('loom.tenancy_mode', 'UPDATE')),
  ('SELECT on loom.cell_self', has_table_privilege('loom.cell_self', 'SELECT')),
  ('SELECT on loom.authority', has_table_privilege('loom.authority', 'SELECT')),
  ('INSERT on loom.tenancy', has_table_privilege('loom.tenancy', 'INSERT')),
  ('SELECT, INSERT, UPDATE and DELETE on loom.cell_probe',
   has_table_privilege('loom.cell_probe', 'SELECT')
   AND has_table_privilege('loom.cell_probe', 'INSERT')
   AND has_table_privilege('loom.cell_probe', 'UPDATE')
   AND has_table_privilege('loom.cell_probe', 'DELETE')),
  ('TEMPORARY on the database', has_database_privilege(current_database(), 'TEMPORARY'))
) v(need, held)
WHERE NOT held
"""


async def own_role_refusals(conn: Any, tables: list[Any]) -> list[Refusal]:
    """What attach's own role lacks, `tables` being the rows `cell_triggers.ORG_TABLES`
    read: its grants, `SELECT` on every org table, and effective owner-role privileges for
    every table it gives triggers (or the trigger pass could not alter it); and a server that
    allows prepared transactions."""
    role = await conn.fetchval("SELECT current_user")
    lacks = [str(r["need"]) for r in await conn.fetch(_OWN_GRANTS)]
    for r in tables:
        if not await conn.fetchval(
            "SELECT has_table_privilege($1, 'SELECT')", f"loom.{r['relname']}"
        ):
            lacks.append(f"SELECT on loom.{r['relname']}")
    planned = set(cell_triggers.planned_names(tables))
    for r in tables:
        if r["relname"] in planned and not r["owned"]:
            lacks.append(f"effective privileges of the owner role of loom.{r['relname']}")
    out = [Refusal(f"postgres role {role}", f"attach's role lacks {n}") for n in lacks]
    out += await prepared_refusals(conn)
    return out


async def prepared_refusals(conn: Any) -> list[Refusal]:
    setting = await conn.fetchval("SELECT current_setting('max_prepared_transactions')::int")
    if setting:
        return [
            Refusal(
                "postgres max_prepared_transactions",
                f"is {setting}: a prepared transaction outlives every session, so step 4 "
                "could not wait for it; set it to 0",
            )
        ]
    return []


# -- the triggers -----------------------------------------------------------------------

_MIGRATION_LOCK = "SELECT pg_advisory_lock($1)"
_MIGRATION_UNLOCK = "SELECT pg_advisory_unlock($1)"


async def ensure_triggers(owner: Owner, wait: Wait, log: Callable[[str], None]) -> list[Refusal]:
    """Give every planned table its triggers (`cell_triggers.ensure_table` with
    `recording_first`), one try at a time under the migration lock, retrying a table whose
    lock is busy a second apart within `wait`; one autovacuum holding it is asked to yield
    by one try just past `deadlock_timeout`. A table this role cannot alter refuses at once.
    Then `missing`: any defect refuses."""
    conn = owner.conn
    deadlock_ms = int(
        await conn.fetchval(
            "SELECT setting::int * CASE unit WHEN 's' THEN 1000 ELSE 1 END "
            "FROM pg_settings WHERE name = 'deadlock_timeout'"
        )
    )
    with wait.counting():
        for table in await cell_triggers.planned_tables(conn):
            if refusal := await _ensure_table(owner, table, deadlock_ms, wait, log):
                return [refusal]
    return [Refusal("postgres triggers", d.text) for d in await cell_triggers.missing(conn)]


async def _ensure_table(
    owner: Owner, table: str, deadlock_ms: int, wait: Wait, log: Callable[[str], None]
) -> Refusal | None:
    conn = owner.conn
    yielded = False
    said = ""
    while True:
        most_ms = _TRIGGER_LOCK_MS
        holders = await cell_triggers.holders(conn, table)
        if not yielded and any(h["backend_type"] == "autovacuum worker" for h in holders):
            most_ms, yielded = deadlock_ms + 500, True
        outcome = await _ensure_one(owner, table, most_ms, wait)
        if outcome.answer == "applied":
            return None
        if outcome.answer == "denied":
            return Refusal(
                f"postgres loom.{table}",
                "attach's role cannot alter it: grant inherited privileges of its owner role "
                "without SET ROLE",
            )
        if not await wait.pause():
            if outcome.holders.startswith(_MIGRATION_HELD):
                return Refusal(
                    f"postgres loom.{table}",
                    f"its triggers were not created within --wait: {outcome.holders}",
                )
            return _busy_refusal(table, await cell_triggers.holders(conn, table))
        # Said again only when the holders change: a retry a second is otherwise silent.
        if outcome.holders != said:
            said = outcome.holders
            log(f"loom.{table}: its lock is busy ({said or 'no holder named'}); retrying")


_MIGRATION_HELD = "the migration lock"


async def _migration_holder(conn: Any, key: int) -> str:
    """`the migration lock (held by …)`, naming the session that holds it."""
    try:
        rows = await conn.fetch(_ADVISORY_HOLDER.format(where=_split(key)))
    except Exception as e:  # noqa: BLE001 — naming the holder is best effort
        return f"{_MIGRATION_HELD} (its holder not read: {type(e).__name__})"
    held = "; ".join(
        f"pid {r['pid']}, {r['application_name'] or 'no name'}, since {r['backend_start']}"
        for r in rows
    )
    return f"{_MIGRATION_HELD} (held by {held or 'a session that just ended'})"


async def _ensure_one(owner: Owner, table: str, most_ms: int, wait: Wait) -> cell_triggers.Outcome:
    """One try under the migration lock: the migration lock's wait, and then the DDL's lock
    wait (at most `most_ms`), each bounded by what `wait` has left when it starts, with the
    try floor. One client deadline covers the lock, DDL and cleanup, including COMMIT or
    rollback. A busy migration lock answers `busy` naming that lock's holder."""
    from server.core.schema.pg_migrate import _LOCK_KEY

    bound_s = (
        wait.bound_ms(_MIGRATION_LOCK_MS) + wait.bound_ms(most_ms)
    ) / 1000 + _TRIGGER_TRY_MARGIN_S
    return await owner.call(
        lambda: _trigger_try(owner.conn, table, most_ms, wait, _LOCK_KEY), total_s=bound_s
    )


async def _trigger_try(
    conn: Any, table: str, most_ms: int, wait: Wait, key: int
) -> cell_triggers.Outcome:
    await conn.execute(f"SET lock_timeout = '{wait.bound_ms(_MIGRATION_LOCK_MS)}ms'")
    try:
        await conn.execute(_MIGRATION_LOCK, key)
    except Exception as e:
        if getattr(e, "sqlstate", None) != "55P03":
            raise
        await conn.execute("RESET lock_timeout")
        return cell_triggers.Outcome("busy", await _migration_holder(conn, key))
    await conn.execute("RESET lock_timeout")
    outcome = await cell_triggers.ensure_table(
        conn,
        table,
        recording_first=True,
        lock_timeout=f"{wait.bound_ms(most_ms)}ms",
        attempts=1,
    )
    await conn.execute(_MIGRATION_UNLOCK, key)
    return outcome


def _busy_refusal(table: str, holders: list[Any]) -> Refusal:
    named = "; ".join(
        f"pid {h['pid']} ({'holds' if h['granted'] else 'waiting'}, {h['backend_type']}, "
        f"{h['application_name'] or 'no name'}, transaction since {h['xact_start']}"
        + (", an anti-wraparound vacuum, which never yields" if h["wraparound"] else "")
        + ")"
        for h in holders
    )
    return Refusal(
        f"postgres loom.{table}",
        f"its triggers' lock was not taken within --wait; held by {named or 'no one now'}",
    )


# -- step 3: the mode commit ------------------------------------------------------------

# Only acknowledged pre-COMMIT contention permits another try on the same session.
_TIMED_OUT = frozenset({"55P03", "57014"})


async def commit_enforced(
    owner: Owner,
    confirm_lock: Callable[[], Any],
    ready: Callable[[], Awaitable[list[str]]],
    wait: Wait,
    log: Callable[[str], None],
    mark: Callable[[str], None],
    *,
    expected_cell: str,
    expected_fp: tuple[dict[str, Any] | None, dict[str, Any] | None],
) -> None:
    """Raise mode on the exact ATTACH-lock backend; an unknown COMMIT ends this run."""
    with wait.counting():
        while True:
            await confirm_lock()
            booting = await ready()
            if not booting:
                done = await _mode_try(
                    owner, wait.bound_ms(_MODE_LOCK_MS), mark, expected_cell, expected_fp
                )
                if done:
                    return
                log("the mode's lock is busy; retrying")
            if not await wait.pause():
                if booting:
                    raise Refused(
                        [
                            Refusal(f"process {p}", "still booting at the end of --wait")
                            for p in booting
                        ]
                    )
                raise Refused(
                    [
                        Refusal(
                            "postgres loom.tenancy_mode",
                            "its lock was not taken within --wait: a long transaction reads "
                            "it FOR SHARE; rerun attach",
                        )
                    ]
                )


async def _mode_try(
    owner: Owner,
    lock_ms: int,
    mark: Callable[[str], None],
    expected_cell: str,
    expected_fp: tuple[dict[str, Any] | None, dict[str, Any] | None],
) -> bool:
    """Keep work, acknowledged contention rollback and cleanup in one try budget."""
    bound_s = (lock_ms + _MODE_STATEMENT_MARGIN_MS) / 1000 + _MODE_STANDBY_S
    return await owner.call(
        lambda: _mode_transaction(owner.conn, lock_ms, mark, expected_cell, expected_fp),
        total_s=bound_s,
    )


async def _mode_transaction(
    conn: Any,
    lock_ms: int,
    mark: Callable[[str], None],
    expected_cell: str,
    expected_fp: tuple[dict[str, Any] | None, dict[str, Any] | None],
) -> bool:
    from server.cli import cell_control

    tx = conn.transaction()
    await tx.start()
    updated = False
    try:
        await conn.execute(f"SET LOCAL lock_timeout = '{lock_ms}ms'")
        await conn.execute(
            f"SET LOCAL statement_timeout = '{lock_ms + _MODE_STATEMENT_MARGIN_MS}ms'"
        )
        await cell_control.check_lock(conn)
        await conn.execute("LOCK TABLE loom.tenancy_mode IN EXCLUSIVE MODE")
        state, first = await read_mode(conn)
        own = await conn.fetchrow("SELECT current_fp, next_fp FROM loom.cell_self")
        observed = None if own is None else (own["current_fp"], own["next_fp"])
        if (
            state != "recording"
            or first != (True, expected_cell)
            or json.dumps(observed, sort_keys=True) != json.dumps(expected_fp, sort_keys=True)
        ):
            raise Refused([Refusal("postgres acceptance rows", "changed: stop and rerun attach")])
        mark("unknown")
        updated = True
        status = await conn.execute("UPDATE loom.tenancy_mode SET mode = 'enforced', since = now()")
        if status != "UPDATE 1":
            raise Refused([Refusal("postgres loom.tenancy_mode", f"the mode update read {status}")])
    except Exception as error:
        if getattr(error, "sqlstate", None) not in _TIMED_OUT:
            raise
        await tx.rollback()
        await cell_control.check_lock(conn)
        if updated:
            mark("recording")
        return False
    await tx.commit()
    mark("enforced")
    return True


# -- the durable read -------------------------------------------------------------------

_DURABLE_TRY_S = 10.0  # not a setting: the state/WAL/COMMIT phase includes its cleanup


@dataclass(frozen=True)
class Durable:
    """What the durable read saw: the mode, the `first` row, and the xid its logical
    message took."""

    mode: str
    first: tuple[bool, str] | None
    xid: int


async def durable_read(owner: Owner) -> Durable:
    """Read and synchronously fence state on the owner's exact ATTACH backend."""
    from server.cli import cell_control

    conn = owner.conn

    async def read() -> Durable:
        await conn.execute("SELECT pg_logical_emit_message(true, 'loom.attach', '')")
        xid = await conn.fetchval("SELECT pg_current_xact_id_if_assigned()::text")
        mode = await conn.fetchval("SELECT mode FROM loom.tenancy_mode")
        first = await conn.fetchrow("SELECT is_first, cell FROM loom.cell_first")
        if xid is None:
            raise Refused([Refusal("postgres durable read", "its logical message took no xid")])
        return Durable(
            mode=str(mode or ""),
            first=None if first is None else (bool(first["is_first"]), str(first["cell"])),
            xid=int(xid),
        )

    return await owner.call(lambda: cell_control.transaction(conn, read), total_s=_DURABLE_TRY_S)


async def snapshot_xmax(conn: Any) -> int:
    return int(await conn.fetchval("SELECT pg_snapshot_xmax(pg_current_snapshot())::text"))


# -- step 4: the transactions that could have read `recording` --------------------------

# Every backend but this statement's own: of the cell's database, one whose xid or (but
# an autovacuum's) snapshot is older than X; of another database, one whose xid is older
# (it holds every snapshot's horizon). Both compared through age() against one reference,
# which is exactly `< X`, wraparound-safe, and takes no xid.
_OPEN = """
WITH x AS (SELECT age($1::text::xid8::xid) AS a)
SELECT s.pid, s.backend_type, s.application_name, s.xact_start::text AS xact_start,
       s.datid = (SELECT oid FROM pg_database WHERE datname = current_database()) AS ours,
       coalesce(s.backend_xid IS NOT NULL AND age(s.backend_xid) > x.a, false) AS xid_old,
       coalesce(s.backend_xmin IS NOT NULL AND age(s.backend_xmin) > x.a
                AND s.backend_type <> 'autovacuum worker', false) AS xmin_old,
       coalesce(s.query = '<insufficient privilege>', false) AS hidden
FROM pg_stat_activity s, x
WHERE s.pid <> pg_backend_pid()
"""


def _named(r: Any) -> str:
    return (
        f"pid {r['pid']} ({r['backend_type']}, {r['application_name'] or 'no name'}, "
        f"transaction since {r['xact_start']})"
    )


async def wait_transactions(conn: Any, xmax: int, wait: Wait) -> None:
    """Wait until no backend of the cell's database holds an xid or a snapshot older than
    `xmax`; Refused at once on a prepared transaction or an unreadable row, and past
    `wait` naming each backend waited on and each other database's horizon holder."""
    with wait.counting():
        await _wait_transactions(conn, xmax, wait)


async def _wait_transactions(conn: Any, xmax: int, wait: Wait) -> None:
    while True:
        if refusals := await prepared_refusals(conn):
            raise Refused(refusals)
        if await conn.fetchval("SELECT count(*) FROM pg_prepared_xacts"):
            raise Refused(
                [Refusal("postgres prepared transaction", "one is open: commit or roll it back")]
            )
        rows = await conn.fetch(_OPEN, str(xmax))
        hidden = [r for r in rows if r["ours"] and r["hidden"]]
        if hidden:
            raise Refused(
                [
                    Refusal(
                        f"postgres backend {r['pid']}",
                        "its transaction state is hidden from attach's role: grant it "
                        "pg_read_all_stats",
                    )
                    for r in hidden
                ]
            )
        waited = [r for r in rows if r["ours"] and (r["xid_old"] or r["xmin_old"])]
        if not waited:
            return
        if not await wait.pause():
            horizon = [r for r in rows if not r["ours"] and r["xid_old"]]
            raise Refused(
                [
                    Refusal(f"postgres {_named(r)}", "may have read `recording`: still open")
                    for r in waited
                ]
                + [
                    Refusal(
                        f"postgres {_named(r)}",
                        "another database's transaction, holding the snapshot horizon",
                    )
                    for r in horizon
                ]
            )


# -- the tenancy rows -------------------------------------------------------------------


async def write_tenancy(owner: Owner, orgs: Iterable[str]) -> int:
    """Write each thousand-org batch on the exact ATTACH backend, returning rows added."""
    batch = sorted(orgs)
    written = 0
    for i in range(0, len(batch), 1000):
        status = await owner.transaction(
            partial(
                owner.conn.execute,
                "INSERT INTO loom.tenancy (org) SELECT unnest($1::text[]) ON CONFLICT DO NOTHING",
                batch[i : i + 1000],
            )
        )
        written += int(status.split()[-1])
    return written


async def read_mode(conn: Any) -> tuple[str, tuple[bool, str] | None]:
    """The cell's state (`cell.read_state`) and its `first` row."""
    state = await cell.read_state(conn)
    first = await conn.fetchrow("SELECT is_first, cell FROM loom.cell_first")
    return state, None if first is None else (bool(first["is_first"]), str(first["cell"]))
