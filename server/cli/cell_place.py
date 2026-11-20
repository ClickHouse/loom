"""Directory-first operator placement, followed by a durable local tenancy row."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from typing import Any

import httpx

from server.cli import cell as cell_cli
from server.cli import cell_attach_pg as apg
from server.cli._ch import run_connected, writes
from server.cli.cell_ch import Refused
from server.cli.cell_control import Owner
from server.core import cell, pg
from server.router.directory import PLACE_MAX_S
from server.settings import settings

# The directory statement bound plus connection and client cleanup.
REQUEST_S = PLACE_MAX_S + 10.0  # not a setting: derived


class PlacementRefused(Exception):
    """A placement cannot complete with this configuration or directory winner."""


def _bound_s() -> float:
    return settings.pg_statement_timeout_ms / 1000 + 1


def _args_problem(args: argparse.Namespace) -> str:
    if not cell.NAME.fullmatch(args.cell or ""):
        return "--cell names 1 to 32 of a-z, 0-9, -"
    if args.cell != settings.cell:
        return f"place runs with the cell's own configuration: set LOOM_CELL={args.cell}"
    if settings.storage_authority != "postgres" or not settings.pg_dsn:
        return "place needs LOOM_STORAGE_AUTHORITY=postgres and the cell's LOOM_PG_DSN"
    if cell.named_org(args.org) is None:
        return "--org names a nonempty Postgres org"
    return cell.org_name_refusal(args.org)


async def _identity(conn: Any, name: str) -> tuple[str, dict[str, Any]]:
    """Read the full local cell state and this CLI's accepted configuration."""
    from server.code.domain.index import repos

    cell.set_code_orgs(repos.operator_orgs)
    state, first = await cell.read_state_if_present(conn)
    if state != "enforced" or first != name:
        raise PlacementRefused(
            f"local cell state is {state!r}, naming {first!r}; place needs enforced {name!r}"
        )
    deployment = await conn.fetchval("SELECT deployment_id FROM loom.authority")
    if not isinstance(deployment, str) or not deployment:
        raise PlacementRefused("the local Postgres records no deployment id")
    own = await conn.fetchrow("SELECT current_fp, next_fp FROM loom.cell_self")
    mine = cell.fingerprint(deployment)
    if own is None or not any(
        cell.matches(mine, cell.json_object(own[k])) for k in ("current_fp", "next_fp")
    ):
        raise PlacementRefused("the CLI's configuration matches no local cell fingerprint")
    return deployment, cell.as_value(mine)


async def _directory(
    router: httpx.URL, name: str, org: str, deployment: str, fingerprint: dict[str, Any]
) -> None:
    url = cell_cli.at(router, f"/cells/v1/operator/cells/{name}/place")
    manager = httpx.AsyncClient(trust_env=router.scheme == "https")
    client = await manager.__aenter__()
    try:
        answer = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.router_operator_token}"},
            json={"org": org, "deployment_id": deployment, "fingerprint": fingerprint},
            timeout=httpx.Timeout(REQUEST_S, connect=5.0),
        )
    except BaseException:  # Client exit preserves the request's first failure.
        with contextlib.suppress(BaseException):
            await manager.__aexit__(*sys.exc_info())
        raise
    else:
        await manager.__aexit__(None, None, None)
    posted = cell_cli.router_answer(answer, {"org": org}, cell_cli.OPERATOR_REFUSALS)
    if posted is None:
        raise PlacementRefused(cell_cli.not_the_router(answer))
    if answer.status_code != 200:
        raise PlacementRefused(f"the router refused ({answer.status_code}): {posted['error']}")
    if posted.get("cell") != name:
        raise PlacementRefused(
            f"org {org!r} belongs to cell {posted.get('cell')!r}; no local tenancy row written"
        )
    if posted.get("deployment_id") != deployment:
        raise PlacementRefused("the router answered another deployment id")


async def place(args: argparse.Namespace, router: httpx.URL) -> dict[str, str]:
    """Hold the operator lock through directory placement and an acknowledged local commit.
    Local phases and the complete directory exchange have separate deadlines."""
    owner = Owner(exit_s=_bound_s())

    async def opening() -> None:
        await owner.connect(statement_timeout_ms=settings.pg_statement_timeout_ms)

    async def initial() -> tuple[str, dict[str, Any]]:
        if refusals := await apg.direct_refusals(owner.conn, "place"):
            raise Refused(refusals)
        await apg.take_lock(owner.conn, "place")
        return await _identity(owner.conn, args.cell)

    async def work() -> dict[str, str]:
        deployment, fingerprint = await owner.call(initial, total_s=_bound_s())
        await owner.call(
            lambda: _directory(router, args.cell, args.org, deployment, fingerprint),
            total_s=REQUEST_S,
        )

        async def local() -> None:
            conn = owner.conn
            tx = conn.transaction()
            await tx.start()
            if not await apg.lock_held(conn):
                raise PlacementRefused("the operator lock was lost")
            if await _identity(conn, args.cell) != (deployment, fingerprint):
                raise PlacementRefused(
                    "the local identity or configuration changed during placement"
                )
            await conn.execute(
                "INSERT INTO loom.tenancy (org) VALUES ($1) ON CONFLICT DO NOTHING", args.org
            )
            # A no-op insert must also flush a primary-only predecessor to the standby.
            await conn.execute(pg._DURABLE_MESSAGE)
            await tx.commit()

        await owner.call(local, total_s=_bound_s())
        return {"org": args.org, "cell": args.cell, "deployment_id": deployment}

    return await owner.run(opening, work, opening_s=_bound_s())


def cmd_place(args: argparse.Namespace) -> int:
    if reason := _args_problem(args):
        print(f"loom cell place: {reason}", file=sys.stderr)
        return 2
    router = cell_cli.operator_router(args)
    if isinstance(router, str):
        print(f"loom cell place: {router}", file=sys.stderr)
        return 2

    async def run(ch: Any) -> int:
        try:
            result = await place(args, router)
        except asyncio.CancelledError:
            print(
                "loom cell place: stopped (CancelledError); "
                "placement may have committed; rerun the same command",
                file=sys.stderr,
            )
            raise
        except Exception as e:  # noqa: BLE001 — an unknown outcome must name its idempotent retry
            print(
                f"loom cell place: stopped ({type(e).__name__}: {e}); "
                "placement may have committed; rerun the same command",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    return run_connected(
        run, org_free=writes("places an org in the directory and records its local tenancy")
    )


def add_place_parser(csub: Any) -> None:
    p = csub.add_parser("place", help="Place an org in this cell and record its tenancy.")
    p.add_argument("--cell", required=True, help="This cell's name (LOOM_CELL)")
    p.add_argument("--org", required=True, help="The org to place")
    p.add_argument("--router", help="The router's URL (default: LOOM_CELL_ROUTER_URL)")
    p.set_defaults(func=cmd_place)
