"""One ClickHouse connection for the whole of a CLI command, and the orgs it writes.

Every command that connects declares what it writes for: `orgs=`, the orgs a command
writes rows for, or `org_free=`, why it writes for none and whether it `reads`, `writes`
(the cell's settings, a sweep of its own rows) or `revokes`. With `LOOM_CELL` set an
org-free write or revocation is refused on another cell's stores (`check_org_free`), a
revocation running through a Postgres outage. On a cell (`LOOM_CELL`), each declared org
must be one the cell holds before the command runs (`server.auth.tenancy`), judged by
the cell's rows read for the command: recorded while the cell is `recording` and the
directory answers that it is `attaching`, refused once it is `enforced` (or the
directory does not say `attaching`), never placed (`loom cell place` places one). With
`LOOM_CELL` unset, under the Postgres authority, a command that declares an org is
refused once its deployment is a cell past `recording` (`check_unset_cell`): its rows
would land unchecked in a cell.
`tests/unit/test_cell_cli_orgs.py` fails on a connect that declares neither, on an
org-free one that does not say which kind it is, and on a connect under `server/cli/`
outside this module.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from server.core.ch import CHClient


class OrgNotHeld(SystemExit):
    """A command declared an org this cell does not hold, or the cell cannot tell: it
    exits with the message, as an operator error does, not with a traceback."""


def dry_run_orgs(args: object) -> tuple[str | None, ...]:
    """The orgs a command with `--dry-run` writes: its `--org`, and none on a dry run,
    which only reads (a read records no org)."""
    return () if getattr(args, "dry_run", False) else (getattr(args, "org", None),)


@dataclass(frozen=True)
class OrgFree:
    """Why a command writes for no org, and what it does to the cell's stores: `read`
    (not gated), `write` (refused unless the cell's `first` row, where one exists, names
    `LOOM_CELL`) or `revocation` (gated as `write`, but run through a Postgres outage)."""

    kind: Literal["read", "write", "revocation"]
    reason: str


def reads(reason: str) -> OrgFree:
    return OrgFree("read", reason)


def writes(reason: str) -> OrgFree:
    return OrgFree("write", reason)


def revokes(reason: str) -> OrgFree:
    return OrgFree("revocation", reason)


def _declared(orgs: Iterable[str | None] | None, org_free: OrgFree | None) -> tuple[str, ...]:
    """The orgs to check. Passing `orgs` declares them, an unset one (a command's
    optional `--org`) naming none; omitting both `orgs` and `org_free` is refused."""
    if orgs is None and org_free is None:
        raise TypeError(
            "a command that connects declares the orgs it writes (orgs=) or why it writes "
            "for none (org_free=)"
        )
    return tuple(o for o in orgs or () if o)


# Bounds on the one state read a CLI makes (an org-declaring one with LOOM_CELL unset, an
# org-free write or revocation with it set), and on its retries when the role's
# connection limit is reached.
_STATE_READ_S = 10.0  # not a setting: a bound on one CLI read, not on serving
_STATE_STATEMENT_MS = 5000  # not a setting: a bound on one CLI read, not on serving
_BUSY_TRIES = 3  # not a setting: a full connection limit clears in moments
_BUSY_PAUSE_S = 0.5  # not a setting: a full connection limit clears in moments


def _connections_full(e: BaseException | None) -> bool:
    """Whether Postgres refused the connection for too many (SQLSTATE 53300), as the
    driver's error or as the cause of the 503 `pg.dedicated` raises."""
    while e is not None:
        if getattr(e, "sqlstate", None) == "53300":
            return True
        e = e.__cause__
    return False


async def _read_state() -> tuple[str, str]:
    from server.core import cell, pg

    tries = 0
    while True:
        try:
            async with pg.dedicated(statement_timeout_ms=_STATE_STATEMENT_MS) as conn:
                return await cell.read_state_if_present(conn)
        except Exception as e:
            # CI jobs share the role's CONNECTION LIMIT: a full one clears in moments.
            tries += 1
            if tries == _BUSY_TRIES or not _connections_full(e):
                raise
            await asyncio.sleep(_BUSY_PAUSE_S * tries)


async def check_unset_cell() -> None:
    """With `LOOM_CELL` unset, refuse an org-declaring command once its deployment is a
    cell past `recording`, as the transports gate a server process with it unset; under
    the ClickHouse authority no cell runs and nothing is read. A failed read refuses."""
    from server.core import cell, pg

    if not pg.enabled():
        return
    try:
        found, name = await asyncio.wait_for(_read_state(), timeout=_STATE_READ_S)
    except pg.AuthorityMismatch:
        raise  # another deployment's Postgres: refused as `check_configured` refuses it
    except Exception as e:  # noqa: BLE001 — an unread cell state refuses the command
        raise OrgNotHeld(
            f"refusing: this deployment's cell state could not be read "
            f"({type(e).__name__}: {_read_failure(e)})"
        ) from None
    if found in ("open", "recording"):
        return
    if found == "partial":
        raise OrgNotHeld(
            "refusing: this deployment holds a partial cell state and LOOM_CELL is unset "
            f"(see {cell.TROUBLESHOOTING}, and loom doctor)"
        )
    raise OrgNotHeld(
        f"refusing: this deployment is a cell in enforced mode (cell {name!r}) and "
        f"LOOM_CELL is unset: set LOOM_CELL={name} for a command that writes an org"
    )


def _read_failure(e: BaseException) -> str:
    """Why a state read failed, as a refusal names it."""
    if isinstance(e, TimeoutError):
        return f"no answer within {_STATE_READ_S:g} s"
    return str(e) or "no reason given"


def _outage(e: BaseException | None) -> bool:
    """Whether a state read failed for a Postgres that does not answer: a lost or refused
    connection or no answer within the bound, never a missing grant."""
    from server.core import pg

    return e is not None and (isinstance(e, pg.PostgresUnavailableError) or pg.is_outage(e))


async def check_org_free(free: OrgFree) -> None:
    """With `LOOM_CELL` set, refuse an org-free `write` or `revocation` command whose
    stores belong to another cell: their `first` row names it. A CLI pointed at another
    cell's stores always finds one there; an absent row, or state tables not migrated
    yet, passes, so a later cell runs these before it is prepared. A failed read refuses,
    but a revocation runs through an outage (docs/architecture/cells.md, "Invariants")."""
    import sys

    from server.core import pg
    from server.settings import settings

    if free.kind == "read" or not settings.cell or not pg.enabled():
        return
    try:
        _found, name = await asyncio.wait_for(_read_state(), timeout=_STATE_READ_S)
    except pg.AuthorityMismatch:
        raise  # another deployment's Postgres: refused as `check_configured` refuses it
    except Exception as e:  # noqa: BLE001 — an unread cell state refuses a write
        if free.kind == "revocation" and _outage(e):
            print(
                f"⚠ this cell's state was not checked: its Postgres did not answer "
                f"({type(e).__name__}); revoking anyway",
                file=sys.stderr,
            )
            return
        raise OrgNotHeld(
            f"refusing: this cell's state could not be read "
            f"({type(e).__name__}: {_read_failure(e)})"
        ) from None
    if name and name != settings.cell:
        raise OrgNotHeld(
            f"refusing: this Postgres's first row names cell {name!r}, not "
            f"LOOM_CELL={settings.cell!r}: the command would change another cell's stores"
        )


async def check_orgs(orgs: Iterable[str]) -> None:
    """Refuse, before the command does anything, an org this cell does not hold."""
    from server.auth import tenancy
    from server.code.domain.index import repos
    from server.core import cell, pg_profile
    from server.core.errors import BackendUnavailableError, ValidationError, WrongCellError
    from server.settings import settings

    orgs = tuple(orgs)
    if not orgs:
        return
    if not settings.cell:
        await check_unset_cell()
        return
    # Boot hands the code orgs' reader in for a server; a CLI process does it here, so
    # its fingerprint is the one its server reports.
    cell.set_code_orgs(repos.operator_orgs)
    # A CLI process has not read the cell's rows. They decide what the transports decide:
    # whether this process may serve at all, and whether a new org is recorded or refused.
    try:
        cell.set_deployment_id(await pg_profile.deployment_id())
    except Exception as e:  # noqa: BLE001 — an unread Postgres refuses the command
        raise OrgNotHeld(f"refusing: this cell's Postgres could not be read ({e})") from None
    await cell.refresh()
    if reason := cell.cli_gate_reason():
        raise OrgNotHeld(f"refusing: {reason}")
    # The router credential can place orgs in the shared directory: a CLI holds it only
    # for the state read a first cell in `recording` makes (a later cell reads `enforced`).
    if settings.cell_router_token and cell.mode() != "recording":
        raise OrgNotHeld(
            "refusing: LOOM_CELL_ROUTER_TOKEN is set in this CLI's configuration, which needs "
            "it only on the first cell while its mode is `recording`; remove it"
        )
    for org in orgs:
        try:
            await tenancy.ensure_held(org, None)
        except WrongCellError as e:
            raise OrgNotHeld(
                f"refusing: {e.message} (`loom cell place --org {org}` places a new org)"
            ) from None
        except (BackendUnavailableError, ValidationError) as e:
            raise OrgNotHeld(f"refusing: {e.message}") from None


async def connect_declared(
    ch: CHClient,
    *,
    orgs: Iterable[str | None] | None = None,
    org_free: OrgFree | None = None,
    check_authority: bool = False,
) -> None:
    """Connect `ch` for a command that writes for `orgs` (or for none, `org_free` saying
    why), refusing, disconnected, an org this cell does not hold. With orgs to check, or
    with `check_authority`, it first refuses a deployment other than its own
    (`check_configured`): an org's rows and its tenancy row land only in this
    deployment's stores, and the cell state is read from this deployment's Postgres."""
    from server.settings import settings

    named = _declared(orgs, org_free)
    # A `loom code` command connects again inside the connect `code_run` made.
    if not ch.connected:
        await ch.connect()
    try:
        # On a cell, a write or a revocation also checks its ClickHouse against its
        # Postgres, so a CLI whose ClickHouse is another cell's is refused with the rest.
        gated = org_free is not None and org_free.kind != "read" and bool(settings.cell)
        if check_authority or named or gated:
            from server.core.pg_profile import check_configured

            await check_configured(ch)
        if org_free is not None and not named:
            await check_org_free(org_free)
        else:
            await check_orgs(named)
    except BaseException:
        await disconnect(ch)
        raise


async def connect_code_plane(ch: CHClient, args: argparse.Namespace) -> None:
    """Connect for a code-index command: it writes the org `code_run._identity` resolves
    (its `--org`, else `LOOM_CODE_ORG`), and on a dry run, which only reads, none. The
    org and namespace are resolved first, so a command that refuses its own arguments
    exits before a `recording` cell records anything."""
    from server.cli.code_run import _identity

    org, _ns = _identity(args)
    await connect_declared(ch, orgs=() if getattr(args, "dry_run", False) else (org,))


async def connect_checked(
    ch: CHClient, *, orgs: Iterable[str | None] | None = None, org_free: OrgFree | None = None
) -> None:
    """`connect_declared`, always refusing a command configured for another memory
    authority than the deployment's, as a replica refuses: an erasure or an import run
    against the wrong one would leave or write the other store's rows."""
    await connect_declared(ch, orgs=orgs, org_free=org_free, check_authority=True)


async def disconnect(ch: CHClient) -> None:
    """Close the Postgres pool a command opened, then `ch`."""
    from server.core import pg

    pg.expect_deployment(None)
    try:
        await pg.close_pool()
    finally:
        await ch.disconnect()


def run_connected[T](
    run: Callable[[CHClient], Awaitable[T]],
    *,
    orgs: Iterable[str | None] | None = None,
    org_free: OrgFree | None = None,
) -> T:
    """`asyncio.run(run(ch))` on a connected client (`connect_checked`, with the orgs the
    command writes), disconnected on every path — an open client at loop close leaks its
    HTTP session."""
    from server import boot
    from server.core.background import register_buffer_drainer
    from server.core.ch import get_client
    from server.insights.app.provider_command import model_command

    named = _declared(orgs, org_free)
    register_buffer_drainer("boot-writes", boot.drain_writes)

    async def _go() -> T:
        ch = get_client()
        await connect_checked(ch, orgs=named, org_free=org_free)
        try:
            async with model_command():
                return await run(ch)
        finally:
            await disconnect(ch)

    return asyncio.run(_go())
