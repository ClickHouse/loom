"""`loom doctor`'s cell checks, run only with LOOM_CELL set (docs/architecture/cells.md):
the deployment check, a partial cell state, the tenancy marker on this process's
sessions and on every session, the defects of the org triggers and the `loom.tenancy`
guard (`cell_triggers.missing`), and the point-in-time reads attach makes, run again on
every doctor run: the login roles that can make an org held or lower the mode, and the
role and database settings and subscriptions that pass an unchecked writer
(`cell_attach_pg`). `--stray-orgs` also reads the orgs the cell's stores hold without a
tenancy row (`stray_org_checks`).

A refusal (ClickHouse access denied, no LOOM_PG_DSN, another recorded authority or a
Postgres of another deployment) fails the doctor's exit and reads no cell state;
any other error is one "not read" line. Configured SCIM policy needs a readable first
identity and fails the run when invalid or unread. Marker and trigger defects are WARN."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from server.cli._ch import reads
from server.cli.doctor import FAIL, PASS, WARN
from server.core import cell_sync

# A doctor run never hangs on a cell's stores: each statement, and each session's connect
# and reads together.
_STATEMENT_MS = 5000  # not a setting: a bound on a diagnostic read, not on serving
_SESSION_S = 10.0  # not a setting: a bound on a diagnostic read, not on serving


class _Refused(Exception):
    """The deployment check refused: the doctor's exit fails."""


async def _deployment_check(ch: Any) -> str:
    """'' when the cell checks may read Postgres; 'before the first boot' when no
    authority is recorded yet. Raises _Refused otherwise."""
    from server.core import pg, pg_profile
    from server.core.ch_errors import error_code
    from server.core.pg import AuthorityMismatch
    from server.core.schema.preflight import ACCESS_DENIED
    from server.settings import settings

    if not settings.pg_dsn:
        raise _Refused(pg.NO_DSN)
    try:
        recorded = await pg_profile.recorded_authority(ch)
        if recorded is None:
            return "before the first boot"
        # The doctor's Postgres sessions refuse a database of another deployment.
        pg.expect_deployment((str(recorded["deployment_id"]), int(recorded["epoch"])))
    except AuthorityMismatch as e:
        raise _Refused(str(e)) from None
    except Exception as e:
        if error_code(e) == ACCESS_DENIED:
            raise _Refused(f"ClickHouse refused the read: {e}") from None
        raise
    return ""


async def _bounded(read: Any) -> Any:
    from server.core.pg import AuthorityMismatch

    try:
        return await asyncio.wait_for(read, timeout=_SESSION_S)
    except AuthorityMismatch as e:
        raise _Refused(str(e)) from None


async def _marked_session() -> tuple[str, tuple[bool, str] | None, bool, list[Any]]:
    """The state and first tuple, whether sessions keep the marker after RESET ALL, and
    the triggers' defects, on one session opened as the pool's are."""
    from server.core import cell, cell_triggers, pg

    async with pg.dedicated(statement_timeout_ms=_STATEMENT_MS) as conn:
        state, first = await cell.read_state_and_first(conn)
        await conn.execute("RESET ALL")  # a startup value survives it, a SET does not
        marked = await conn.fetchval("SELECT current_setting($1, true)", pg.TENANCY_MARKER)
        defects = await cell_triggers.missing(conn)
    return state, first, marked == "on", defects


async def _unmarked_session() -> bool:
    """Whether a session that sends no marker passes the triggers all the same (a role,
    database, ALTER SYSTEM or options setting): marked, or in the replica role that fires
    no origin trigger."""
    from server.cli import cell_attach_pg

    return await cell_attach_pg.session_passes()


async def _writer_reads(allowed_roles: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Attach's point-in-time Postgres reads, again: the login roles outside the owner
    roles, the superusers and `--pg-allow-role` that can make an org held or lower the
    mode, and the settings and subscriptions that pass an unchecked writer."""
    from server.cli import cell_attach_pg
    from server.core import pg

    async with pg.dedicated(statement_timeout_ms=_STATEMENT_MS) as conn:
        holders = await cell_attach_pg.privilege_holders(conn, exempt_self=False)
        unchecked = await cell_attach_pg.unmarked_refusals(conn, False)
    roles = cell_attach_pg.privilege_refusals(holders, allowed_roles)
    return [
        (
            "cell_tenancy_privilege",
            WARN if roles else PASS,
            "; ".join(str(r) for r in roles) or "no role outside the owner roles can",
        ),
        (
            "cell_unchecked_writers",
            WARN if unchecked else PASS,
            "; ".join(str(r) for r in unchecked) or "no setting or subscription passes one",
        ),
    ]


def _grades(
    state: str, marked: bool, marks_all: bool, defects: list[Any]
) -> list[tuple[str, str, str]]:
    from server.core import cell

    out: list[tuple[str, str, str]] = []
    if state == "partial":
        out.append(
            (
                "cell_state",
                WARN,
                f"this cell holds a partial state; see {cell.TROUBLESHOOTING}",
            )
        )
    else:
        out.append(("cell_state", PASS, state))
    if marked:
        out.append(("cell_marker", PASS, "this process's sessions are marked"))
    else:
        if state == "enforced":
            effect = (
                "Loom's own writes for a stray org, and its tenancy row for each newly "
                "placed org, are refused"
            )
        elif state == "partial":
            effect = (
                "Loom's own writes for a stray org, and its tenancy rows, are refused where "
                "the org triggers and the loom.tenancy guard exist"
            )
        else:
            effect = (
                "once the cell is enforced, Loom's own writes for a stray org, and its "
                "tenancy row for each newly placed org, will be refused"
            )
        out.append(
            (
                "cell_marker",
                WARN,
                "this process's sessions lose loom.tenancy_checked (a pooler or proxy drops the "
                "startup parameter): "
                f"{effect}; point LOOM_PG_DSN straight at Postgres",
            )
        )
    if marks_all:
        out.append(
            (
                "cell_marker_everywhere",
                WARN,
                "a session that sends no marker is marked, or its session_replication_role is "
                "replica (a role, database, ALTER SYSTEM or options setting): the org "
                "triggers and the loom.tenancy guard pass every writer",
            )
        )
    named = [d.text for d in defects if d.kind != "absent"]
    if state == "enforced":
        # Normal before the cell's first migrating boot; after it, a failed `ensure`.
        named += [d.text + " (a migrating boot creates it)" for d in defects if d.kind == "absent"]
    out.append(("cell_triggers", WARN if named else PASS, "; ".join(named) or "as expected"))
    return out


async def _checks(ch: Any, allowed_roles: tuple[str, ...] = ()) -> list[tuple[str, str, str]]:
    from server.cli.doctor import _doctor_scim_token_check
    from server.settings import settings

    try:
        if before := await asyncio.wait_for(_deployment_check(ch), timeout=_SESSION_S):
            return [("cell_state", PASS, f"cell checks not run: {before}")] + _unread_scim()
        state, first, marked, defects = await _bounded(_marked_session())
        marks_all = await _bounded(_unmarked_session())
    except _Refused as e:
        return [("cell_deployment", FAIL, f"deployment check refused: {e}")] + _unread_scim()
    except Exception as e:  # noqa: BLE001 — an unreadable cell is named, not a failed run
        return [("cell_state", WARN, f"not read: {_reason(e)}")] + _unread_scim()
    try:
        reads: list[tuple[str, str, str]] = await _bounded(_writer_reads(allowed_roles))
    except Exception as e:  # noqa: BLE001 — an unread catalog is named, not a failed run
        reads = [
            (name, WARN, f"not read: {_reason(e)}")
            for name in ("cell_tenancy_privilege", "cell_unchecked_writers")
        ]
    return (
        _grades(state, marked, marks_all, defects)
        + reads
        + [_doctor_scim_token_check(settings, first)]
    )


def _unread_scim() -> list[tuple[str, str, str]]:
    from server.cli.doctor import _doctor_scim_token_check
    from server.core.scim_tokens import tokens
    from server.settings import settings

    return (
        [_doctor_scim_token_check(settings)]
        if settings.cell and tokens(settings.scim_token)
        else []
    )


def _reason(e: BaseException) -> str:
    """An error for a "not read" line; a bound that ran out raises one with no text."""
    return f"{type(e).__name__}: {str(e) or 'no answer within the bound'}"


def cell_checks(allowed_roles: tuple[str, ...] = ()) -> list[tuple[str, str, str]]:
    """The cell checks, on the CLI's ClickHouse client connected for them alone (no CLI
    deployment check: a deployment not booted yet is named, not refused). `allowed_roles`
    is doctor's `--pg-allow-role`."""
    from server.cli._ch import connect_declared, disconnect
    from server.core.ch import get_client

    async def _go() -> list[tuple[str, str, str]]:
        ch = get_client()
        try:
            await asyncio.wait_for(
                connect_declared(ch, org_free=reads("loom doctor reads a cell's state")),
                timeout=_SESSION_S,
            )
            return await _checks(ch, allowed_roles)
        except Exception as e:  # noqa: BLE001 — a ClickHouse that does not answer is named
            return [("cell_state", WARN, f"not read: {_reason(e)}")] + _unread_scim()
        finally:
            first = sys.exception()
            try:
                await disconnect(ch)
            except BaseException:  # keep the diagnostic failure when close also fails
                if first is None:
                    raise

    return asyncio.run(_go())


# -- --stray-orgs --------------------------------------------------------------------------


async def _table_sizes(ch: Any, tables: list[Any]) -> dict[str, int]:
    """Each ClickHouse table's active row count, from `system.parts` (KeeperMap: 0)."""
    from server.core import ch_names

    rows = await ch.query_all(
        "SELECT database, table, sum(rows) AS n FROM system.parts WHERE active "
        "AND database IN {dbs:Array(String)} GROUP BY database, table",
        {"dbs": sorted({t.database for t in tables})},
    )
    return {f"{ch_names.logical(str(r['database']))}.{r['table']}": int(r["n"]) for r in rows}


async def _stray_reads(ch: Any, budget_s: float) -> list[tuple[str, str, str]]:
    from server.cli import cell_scan
    from server.core import pg
    from server.core.cell_ch import Refused, databases, read_topology
    from server.core.cell_triggers import ORG_TABLES

    topology = await read_topology(ch)
    if topology.multi:
        from server.insights.store.cell_catalog import host_names

        names = await host_names(ch, topology)
    else:
        names = {}
    listed, problems = await cell_scan.list_clickhouse(ch, topology, names, databases())
    ch_tables = [t for t in listed if t.engine.endswith("MergeTree") or t.engine == "KeeperMap"]
    sizes = await _table_sizes(ch, ch_tables)
    async with pg.dedicated(statement_timeout_ms=_STATEMENT_MS) as conn:
        held = {str(r["org"]) for r in await conn.fetch("SELECT org FROM loom.tenancy")}
        pg_rows = await conn.fetch(
            "SELECT 'loom.' || c.relname AS name, greatest(c.reltuples, 0)::bigint AS n "
            "FROM pg_class c WHERE c.oid IN (SELECT to_regclass('loom.' || t.relname) FROM ("
            + ORG_TABLES
            + ") t)"
        )
    # Smallest first: a budget that runs out leaves the largest unchecked.
    work: list[tuple[int, str, Any]] = [(int(r["n"]), "postgres", str(r["name"])) for r in pg_rows]
    work += [(sizes.get(t.canonical, 0), "clickhouse", t) for t in ch_tables]
    work.sort(key=lambda w: (w[0], str(w[2])))
    budget = cell_sync.Budget(budget_s)
    stray: dict[str, list[str]] = {}
    unchecked: list[str] = []
    refused: list[str] = []
    for i, (_n, store, table) in enumerate(work):
        found = cell_scan.ScanResult()
        name = table if store == "postgres" else table.canonical
        try:
            if store == "postgres":
                for org in await cell_scan.pg_orgs(table, budget):
                    found.add(name, org)
            else:
                for raw in await cell_scan.ch_orgs(ch, table, topology, budget):
                    found.add(name, raw)
        except Refused as e:
            # The budget spent leaves this table and the larger ones unread; any other
            # refusal (a table with no org-led index, a replica that cannot sync) names
            # this table alone, and the read goes on.
            if budget.left() <= 1:
                unchecked += [w[2] if w[1] == "postgres" else w[2].canonical for w in work[i:]]
                break
            refused += [str(r) for r in e.refusals]
            continue
        orgs = sorted(set(found.orgs) - held) + sorted(found.too_long)
        bad = [m for ms in found.malformed.values() for m in ms]
        if orgs or bad:
            log = " (a log table: never placed)" if name in cell_scan.LOG_TABLES else ""
            stray[f"{store} {name}{log}"] = orgs[:5] + bad[:5]
    out = [
        (
            "cell_stray_orgs",
            WARN if stray else PASS,
            "; ".join(f"{t}: {', '.join(o)}" for t, o in sorted(stray.items()))
            or "every org found has a tenancy row",
        )
    ]
    if unchecked:
        out.append(
            (
                "cell_stray_orgs_unchecked",
                WARN,
                f"not read within --stray-orgs-budget ({budget_s:g} s): " + ", ".join(unchecked),
            )
        )
    if refused or problems:
        out.append(
            (
                "cell_stray_orgs_unread",
                WARN,
                "not read: " + "; ".join(refused + [str(p) for p in problems]),
            )
        )
    return out


def stray_org_checks(budget_s: float) -> list[tuple[str, str, str]]:
    """`loom doctor --stray-orgs`: the orgs the cell's stores hold without a tenancy row
    (log-only orgs, names no request can carry, rows an unchecked writer left), smallest
    tables first within one budget, naming every table left unchecked and every table it
    could not read. Never a FAIL: the operator removes what it names by hand
    (docs/operations/cell-attach.md)."""
    from server.cli._ch import connect_declared, disconnect
    from server.core.ch import get_client, statement_timeout

    async def _go() -> list[tuple[str, str, str]]:
        ch = get_client()
        try:
            await asyncio.wait_for(
                connect_declared(ch, org_free=reads("loom doctor reads a cell's orgs")),
                timeout=_SESSION_S,
            )
            if before := await asyncio.wait_for(_deployment_check(ch), timeout=_SESSION_S):
                return [("cell_stray_orgs", PASS, f"not read: {before}")]
            return await _stray_reads(ch, budget_s)
        except Exception as e:  # noqa: BLE001 — an unread store is named, not a failed run
            return [("cell_stray_orgs", WARN, f"not read: {_reason(e)}")]
        finally:
            first = sys.exception()
            try:
                await disconnect(ch)
            except BaseException:  # keep the diagnostic failure when close also fails
                if first is None:
                    raise

    with statement_timeout(budget_s):
        return asyncio.run(_go())
