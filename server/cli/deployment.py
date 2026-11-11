"""`loom deployment publish`: publish this env's configuration as the
deployment's, the one step that replaces its root secret or a MUST_MATCH value
(`server.core.deployment_config`). Every replica that still runs the old one
stops being ready until it is restarted with this env.
"""

from __future__ import annotations

import argparse
import socket
import sys
from typing import Any

from server.cli._ch import run_connected


def _cmd_publish(args: argparse.Namespace) -> int:
    from server.core import deployment_config
    from server.settings import settings

    if not settings.deployment:
        print("✗ run with the deployment's env (LOOM_DEPLOYMENT=1)", file=sys.stderr)
        return 2

    async def _go(ch: Any) -> int:
        stored = await deployment_config.read_published(ch)
        changes = deployment_config.compare(settings, stored) if stored else []
        for change in changes:
            print(f"  • {change}")
        if args.check:
            if changes:
                print("✗ this env differs from the published configuration", file=sys.stderr)
                return 2
            print("✓ this env matches the published configuration")
            return 0
        if changes and not args.yes:
            print(
                "✗ the replicas that run the published configuration stop being ready "
                "until they run this env; pass --yes to publish it",
                file=sys.stderr,
            )
            return 2
        mine = deployment_config.snapshot(settings)
        if stored == mine:
            print("✓ already published")
            return 0
        try:
            await deployment_config.write(ch, mine, published_by=f"cli:{socket.gethostname()}")
        except Exception as e:  # noqa: BLE001 — reported with a failing exit
            print(f"✗ publish failed: {e}", file=sys.stderr)
            return 1
        print("✓ published")
        return 0

    return run_connected(_go, org_free="the deployment's published configuration")


def register_deployment_cli(sub: Any) -> None:
    p = sub.add_parser("deployment", help="The configuration every replica checks itself against.")
    dsub = p.add_subparsers(dest="deployment_cmd", required=True)
    pub = dsub.add_parser(
        "publish",
        help="Publish this env's configuration as the deployment's, replacing its root "
        "secret or LOOM_EMBEDDING_MODEL/DIM on purpose.",
    )
    pub.add_argument("--yes", action="store_true", help="Replace what must match")
    pub.add_argument(
        "--check",
        action="store_true",
        help="Write nothing; exit 2 when this env differs from the published configuration",
    )
    pub.set_defaults(func=_cmd_publish)
