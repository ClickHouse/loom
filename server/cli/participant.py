"""`loom participant retire`: the manual recovery for a Loom process that died
without shutting down.

Every Loom process registers a provider participant row, and a provider
reconfiguration waits until each live participant acknowledges the drain. A
participant that stops beating expires by itself; this retires one at once,
once an operator has confirmed its process is gone.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any


def _cmd_retire(args: argparse.Namespace) -> int:
    """Retire a provider participant row whose process died without shutting
    down, so a provider reconfiguration need not wait for it to expire."""
    from uuid import UUID

    from server.cli._ch import run_connected

    try:
        participant = UUID(args.participant)
    except ValueError:
        print(f"  ✗ {args.participant!r} is not a participant id", file=sys.stderr)
        return 2
    if not args.yes:
        print(
            "  ✗ retiring a participant that is still running lets a reconfiguration proceed "
            "under its writes; confirm its process is stopped, then pass --yes",
            file=sys.stderr,
        )
        return 2

    async def _go(ch: Any) -> str:
        from server.insights.app.provider_barrier import retire_participant

        return await retire_participant(participant, ch=ch)

    outcome = run_connected(_go, org_free="a participant row of this cell")
    if outcome == "absent":
        print(f"  ✗ no live participant {participant}", file=sys.stderr)
        return 1
    if outcome == "live":
        print(
            f"  ✗ participant {participant} is still running: its heartbeat is fresh or "
            "a query of its own still runs",
            file=sys.stderr,
        )
        return 1
    print(f"  ✓ retired participant {participant}")
    return 0


def register_participant_cli(sub: Any) -> None:
    p = sub.add_parser("participant", help="The Loom processes a provider change waits for.")
    ssub = p.add_subparsers(dest="participant_cmd", required=True)
    r = ssub.add_parser(
        "retire",
        help="Retire the provider participant of a process that died without shutting "
        "down (its id is `participant_id` in that process's /v1/health).",
    )
    r.add_argument("--participant", required=True, help="The participant id to retire")
    r.add_argument("--yes", action="store_true", help="Its process is confirmed stopped")
    r.set_defaults(func=_cmd_retire)
