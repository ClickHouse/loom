"""Operator configuration updates and verified address refresh for a prepared cell."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any

from server.cli import cell as cell_cli
from server.cli._ch import run_control_connected, writes
from server.cli.cell_prepare import _WAIT_MAX_S
from server.core import cell
from server.settings import settings


def _args_problem(args: argparse.Namespace) -> str:
    if not cell.NAME.fullmatch(args.cell or ""):
        return "--cell names 1 to 32 of a-z, 0-9, -"
    if settings.cell != args.cell:
        return f"update runs with this cell's configuration: set LOOM_CELL={args.cell}"
    if settings.storage_authority != "postgres" or not settings.pg_dsn:
        return "update needs LOOM_STORAGE_AUTHORITY=postgres and this cell's LOOM_PG_DSN"
    if not math.isfinite(args.wait) or not 0 <= args.wait <= _WAIT_MAX_S:
        return f"--wait is finite, nonnegative and at most {_WAIT_MAX_S} seconds"
    if sum(bool(value) for value in (args.config, args.finish, args.revert, args.probe_url)) > 1:
        return "choose one of --config, --finish, --revert or --probe-url"
    if getattr(args, "revoke_unprovisioned", False) and not (
        args.config or args.finish or args.revert
    ):
        return "--revoke-unprovisioned needs --config, --finish or --revert"
    return ""


def cmd_update(args: argparse.Namespace) -> int:
    if reason := _args_problem(args):
        print(f"loom cell update: {reason}", file=sys.stderr)
        return 2
    router = cell_cli.operator_router(args)
    if isinstance(router, str):
        print(f"loom cell update: {router}", file=sys.stderr)
        return 2

    async def run(ch: Any) -> int:
        from server.cli.cell_update_run import update

        try:
            answer = await update(ch, args, router)
        except asyncio.CancelledError:
            print(
                "loom cell update: interrupted; outcome may be unknown; rerun the same action",
                file=sys.stderr,
            )
            raise
        except Exception as error:  # noqa: BLE001 — failures must identify the bounded recovery action.
            print(
                f"loom cell update: stopped ({type(error).__name__}: {error}); "
                "rerun the same action",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(answer, indent=2, sort_keys=True))
        return 0

    return run_control_connected(
        run, org_free=writes("updates this cell's reserved configuration and verified addresses")
    )


def add_update_parser(csub: Any) -> None:
    parser = csub.add_parser(
        "update",
        help="Reserve configuration, finish or revert its rollout, or refresh verified addresses.",
    )
    parser.add_argument("--cell", required=True, help="This cell's name (LOOM_CELL)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--config",
        type=Path,
        help=(
            "Nonsecret version: 1 settings YAML overlaid on this CLI's effective settings; "
            "does not install the file"
        ),
    )
    action.add_argument(
        "--finish",
        action="store_true",
        help="Promote next after durable local narrowing and permanent participant turnover",
    )
    action.add_argument(
        "--revert",
        action="store_true",
        help=(
            "Retain current after durable local narrowing and permanent participant "
            "turnover; keep placements"
        ),
    )
    action.add_argument(
        "--probe-url",
        help=(
            "Later cell's verified probe listener URL; no flag refreshes the first cell's "
            "router-configured addresses"
        ),
    )
    parser.add_argument(
        "--revoke-unprovisioned",
        action="store_true",
        help="Cut off pre-update tokens of subjects that cannot satisfy the target SCIM policy",
    )
    parser.add_argument("--router", help="The router's URL (default: LOOM_CELL_ROUTER_URL)")
    parser.add_argument(
        "--wait",
        type=float,
        default=600,
        help=(
            "Seconds to wait for probe readiness or participant turnover (0 to 86400, default: 600)"
        ),
    )
    parser.epilog = (
        "Finish/revert require foundation-capable runtimes and completed "
        "positive legacy-process turnover. A census cannot verify that external "
        "inventory. Address refresh requires every router updated first; retain "
        "old endpoints until every router refreshes or restarts after the "
        "directory change."
    )
    parser.set_defaults(func=cmd_update)
