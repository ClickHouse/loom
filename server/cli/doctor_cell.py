"""`loom doctor`'s cell checks, run only with LOOM_CELL set (docs/architecture/cells.md):
the deployment check, a partial cell state, the tenancy marker on this process's
sessions and on every session, and the org triggers' defects (`cell_triggers.missing`).

A refusal (ClickHouse access denied, no LOOM_PG_DSN, another recorded authority or a
Postgres of another deployment) fails the doctor's exit and reads no cell state;
any other error is one "not read" line. None of the rest fails a doctor run used as a
deployment gate: a defect is named, graded WARN."""

from __future__ import annotations

import asyncio
from typing import Any

from server.cli._ch import reads
from server.cli.doctor import FAIL, PASS, WARN

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


async def _marked_session() -> tuple[str, bool, list[Any]]:
    """The state, whether this process's sessions keep the marker after RESET ALL, and
    the triggers' defects, on one session opened as the pool's are."""
    from server.core import cell, cell_triggers, pg

    async with pg.dedicated(statement_timeout_ms=_STATEMENT_MS) as conn:
        state = await cell.read_state(conn)
        await conn.execute("RESET ALL")  # a startup value survives it, a SET does not
        marked = await conn.fetchval("SELECT current_setting($1, true)", pg.TENANCY_MARKER)
        defects = await cell_triggers.missing(conn)
    return state, marked == "on", defects


async def _unmarked_session() -> bool:
    """Whether a session that sends no marker passes the triggers all the same (a role,
    database, ALTER SYSTEM or options setting): marked, or in the replica role that fires
    no origin trigger."""
    from server.core import pg

    async with pg.dedicated(statement_timeout_ms=_STATEMENT_MS, marked=False) as conn:
        value: str | None = await conn.fetchval(
            "SELECT current_setting($1, true)", pg.TENANCY_MARKER
        )
        role: str = await conn.fetchval("SELECT current_setting('session_replication_role')")
    return value == "on" or role == "replica"


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
            effect = "Loom's own writes for a stray org are refused"
        elif state == "partial":
            effect = "Loom's own writes for a stray org are refused where the org triggers exist"
        else:
            effect = "once the cell is enforced, Loom's own writes for a stray org will be refused"
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
                "triggers pass every writer",
            )
        )
    named = [d.text for d in defects if d.kind != "absent"]
    if state == "enforced":
        # Normal before the cell's first migrating boot; after it, a failed `ensure`.
        named += [d.text + " (a migrating boot creates it)" for d in defects if d.kind == "absent"]
    out.append(("cell_triggers", WARN if named else PASS, "; ".join(named) or "as expected"))
    return out


async def _checks(ch: Any) -> list[tuple[str, str, str]]:
    try:
        if before := await asyncio.wait_for(_deployment_check(ch), timeout=_SESSION_S):
            return [("cell_state", PASS, f"cell checks not run: {before}")]
        state, marked, defects = await _bounded(_marked_session())
        marks_all = await _bounded(_unmarked_session())
    except _Refused as e:
        return [("cell_deployment", FAIL, f"deployment check refused: {e}")]
    except Exception as e:  # noqa: BLE001 — an unreadable cell is named, not a failed run
        return [("cell_state", WARN, f"not read: {_reason(e)}")]
    return _grades(state, marked, marks_all, defects)


def _reason(e: BaseException) -> str:
    """An error for a "not read" line; a bound that ran out raises one with no text."""
    return f"{type(e).__name__}: {str(e) or 'no answer within the bound'}"


def cell_checks() -> list[tuple[str, str, str]]:
    """The cell checks, on the CLI's ClickHouse client connected for them alone (no CLI
    deployment check: a deployment not booted yet is named, not refused)."""
    from server.cli._ch import connect_declared, disconnect
    from server.core.ch import get_client

    async def _go() -> list[tuple[str, str, str]]:
        ch = get_client()
        try:
            await asyncio.wait_for(
                connect_declared(ch, org_free=reads("loom doctor reads a cell's state")),
                timeout=_SESSION_S,
            )
            return await _checks(ch)
        except Exception as e:  # noqa: BLE001 — a ClickHouse that does not answer is named
            return [("cell_state", WARN, f"not read: {_reason(e)}")]
        finally:
            await disconnect(ch)

    return asyncio.run(_go())
