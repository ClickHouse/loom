"""`loom provider-lock clear`: free the provider configuration lock of a holder
that cannot be taken over by itself.

A boot or a reconfiguration takes over an expired lock once its holder's
participant heartbeat expired. A holder from a build without heartbeats, or
one with no participant row, never counts as stopped; an operator who has
confirmed its process is gone clears it here.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

_REFUSED = {
    "absent": "the lock is not held by {holder}",
    "live": "{holder} renewed the lock within its TTL or its participant still heartbeats",
    "running": "a ClickHouse query of {holder}'s process is still running",
}


def _cmd_clear(args: argparse.Namespace) -> int:
    from server.core.schema.migrate import client
    from server.insights.store.provider_lock import clear_holder

    ch = client(connect_timeout_s=5)
    try:
        outcome = clear_holder(ch, args.holder)
    finally:
        ch.close()
    if outcome != "cleared":
        print(f"  ✗ not cleared: {_REFUSED[outcome].format(holder=args.holder)}", file=sys.stderr)
        return 1
    print(f"  ✓ cleared the provider configuration lock of {args.holder}")
    return 0


def register_provider_lock_cli(sub: Any) -> None:
    p = sub.add_parser("provider-lock", help="The provider configuration lock.")
    ssub = p.add_subparsers(dest="provider_lock_cmd", required=True)
    c = ssub.add_parser(
        "clear",
        help="Free the provider configuration lock of a holder whose process stopped "
        "(refused while it renews, heartbeats or has a query running).",
    )
    c.add_argument(
        "--holder",
        required=True,
        help="The holder the lock names (`holder` in loom._migration_lock, "
        "name = 'provider_configuration')",
    )
    c.set_defaults(func=_cmd_clear)
