"""One ClickHouse connection for the whole of a CLI command, and the orgs it writes.

Every command that connects declares what it writes for: `orgs=`, the orgs a command
writes rows for, or `org_free=`, why it writes for none (the cell's settings, a sweep of
its own rows, a read). On a cell (`LOOM_CELL`), each declared org must be one the cell
holds before the command runs (`server.auth.tenancy`), judged by the cell's rows read for
the command: recorded while the cell is `recording`, refused once it is `enforced`, never
placed (`loom cell place` places one). `tests/unit/test_cell_cli_orgs.py` fails on a
connect that declares neither, and on a connect under `server/cli/` outside this module.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.core.ch import CHClient


class OrgNotHeld(SystemExit):
    """A command declared an org this cell does not hold, or the cell cannot tell: it
    exits with the message, as an operator error does, not with a traceback."""


def dry_run_orgs(args: object) -> tuple[str | None, ...]:
    """The orgs a command with `--dry-run` writes: its `--org`, and none on a dry run,
    which only reads (a read records no org)."""
    return () if getattr(args, "dry_run", False) else (getattr(args, "org", None),)


def _declared(orgs: Iterable[str | None] | None, org_free: str) -> tuple[str, ...]:
    """The orgs to check. Passing `orgs` declares them, an unset one (a command's
    optional `--org`) naming none; omitting both `orgs` and `org_free` is refused."""
    if orgs is None and not org_free:
        raise TypeError(
            "a command that connects declares the orgs it writes (orgs=) or why it writes "
            "for none (org_free=)"
        )
    return tuple(o for o in orgs or () if o)


async def check_orgs(orgs: Iterable[str]) -> None:
    """Refuse, before the command does anything, an org this cell does not hold."""
    from server.auth import tenancy
    from server.core import cell, pg_profile
    from server.core.errors import BackendUnavailableError, WrongCellError
    from server.settings import settings

    orgs = tuple(orgs)
    if not settings.cell or not orgs:
        return
    # A CLI process has not read the cell's rows. They decide what the transports decide:
    # whether this process may serve at all, and whether a new org is recorded or refused.
    from server.code.domain.index import repos

    cell.set_code_orgs(repos.operator_orgs)
    try:
        cell.set_deployment_id(await pg_profile.deployment_id())
    except Exception as e:  # noqa: BLE001 — an unread Postgres refuses the command
        raise OrgNotHeld(f"refusing: this cell's Postgres could not be read ({e})") from None
    await cell.refresh()
    if reason := cell.gate_reason():
        raise OrgNotHeld(f"refusing: this cell serves nothing now: {reason}")
    for org in orgs:
        try:
            await tenancy.ensure_held(org, None)
        except WrongCellError as e:
            raise OrgNotHeld(
                f"refusing: {e.message} (`loom cell place --org {org}` places a new org)"
            ) from None
        except BackendUnavailableError as e:
            raise OrgNotHeld(f"refusing: {e.message}") from None


async def connect_declared(
    ch: CHClient,
    *,
    orgs: Iterable[str | None] | None = None,
    org_free: str = "",
    check_authority: bool = False,
) -> None:
    """Connect `ch` for a command that writes for `orgs` (or for none, `org_free` saying
    why), refusing, disconnected, an org this cell does not hold. On a cell with orgs to
    check, or with `check_authority`, it first refuses a deployment other than its own
    (`check_configured`): a tenancy row is written only into this deployment's Postgres."""
    from server.settings import settings

    named = _declared(orgs, org_free)
    # A `loom code` command connects again inside the connect `code_run` made.
    if not ch.connected:
        await ch.connect()
    try:
        if check_authority or (settings.cell and named):
            from server.core.pg_profile import check_configured

            await check_configured(ch)
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
    ch: CHClient, *, orgs: Iterable[str | None] | None = None, org_free: str = ""
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
    org_free: str = "",
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
