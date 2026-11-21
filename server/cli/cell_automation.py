"""Provision the two public bot purposes through a fenced operator mutation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from server.cli import cell_bot_proof, cell_control
from server.router import cell_admin


async def provision(
    owner: cell_control.Owner, router: Any, args: argparse.Namespace, transport: Any = None
) -> dict[str, Any]:
    """Prove both live keys while one native control owner holds the cell lock."""

    async def call(path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await cell_control.request(router, args.cell, transport, path, body)

    row = await cell_control.request(router, args.cell, transport, "", method="GET")
    if row.get("state") != "serving" or row.get("mode") != "enforced":
        raise cell_control._refused("automation provisioning requires a serving, enforced cell")
    row = await cell_control.claim(owner.conn, call, row)
    automation = cell_bot_proof.BotKey(
        args.automation_bot, args.automation_thumbprint, args.automation_key
    )
    recovery = cell_bot_proof.BotKey(args.recovery_bot, args.recovery_thumbprint, args.recovery_key)
    bots = await owner.call(
        lambda: cell_bot_proof.prove_pair(
            cell=row["cell"],
            deployment_id=row["deployment_id"],
            origin="https://" + row["host"],
            automation=automation,
            recovery=recovery,
            ca_file=args.ca_file,
        ),
        total_s=35.0,
    )
    public = cell_admin.public_pair(json.dumps(bots, ensure_ascii=True, separators=(",", ":")))
    answer = await cell_control.mutate(owner.conn, call, row, "/administration", {"bots": bots})
    expected = {"version": 1, "cell": row["cell"], "deployment_id": row["deployment_id"], **public}
    if answer != expected:
        raise cell_control._refused(
            "automation metadata reply unknown: stop and read the directory"
        )
    return answer


def command(args: argparse.Namespace) -> int:
    from server.cli import cell

    router = cell._cell_router(args)
    if isinstance(router, int):
        return router
    try:
        answer = asyncio.run(
            cell_control.run("automation", lambda owner: provision(owner, router, args))
        )
    except (cell_control.Refused, ValueError, OSError, TimeoutError, httpx.HTTPError) as error:
        # Exception messages from key files and transports can contain private paths.
        print(
            f"loom cell automation: refused ({type(error).__name__}); "
            "read the directory before retrying",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 0


def add_automation_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "automation", help="Prove and register distinct automation and recovery bot keys."
    )
    parser.add_argument("--cell", required=True, help="The serving cell to provision")
    parser.add_argument("--router", help="The router's URL (default: LOOM_CELL_ROUTER_URL)")
    for purpose in ("automation", "recovery"):
        parser.add_argument(
            f"--{purpose}-bot", required=True, help=f"The {purpose} bot's exact registered name"
        )
        parser.add_argument(
            f"--{purpose}-thumbprint",
            required=True,
            help=f"The {purpose} bot's approved public key thumbprint",
        )
        parser.add_argument(
            f"--{purpose}-key",
            required=True,
            type=Path,
            help=f"The {purpose} bot's private key file",
        )
    parser.add_argument(
        "--ca-file", type=Path, help="Private file containing an explicit CA certificate bundle"
    )
    parser.set_defaults(func=command)
