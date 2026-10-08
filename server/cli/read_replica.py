"""`loom read-replica retire`: the recovery for a read-replica that died
without shutting down.

Every Loom process registers a provider participant row, and a provider
reconfiguration on the main Loom waits until each live participant
acknowledges the drain. A read-replica on another host that was killed (an
OOM, a lost machine) leaves its row behind, so an operator who has confirmed
the process is gone retires it here.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any


def _cmd_retire(args: argparse.Namespace) -> int:
    """Retire a read-replica's provider participant row after its process died
    without shutting down. The main Loom cannot prove a process on another host
    stopped, so a provider reconfiguration waits on that row until an operator
    who has confirmed the process is gone retires it."""
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

    async def _go(ch: Any) -> bool:
        from server.insights.store.provider_participants import participants, record_participant

        row = next(
            (r for r in await participants(ch=ch) if str(r["participant_id"]) == str(participant)),
            None,
        )
        if row is None:
            return False
        await record_participant(
            participant, UUID(int=0), "retired", after=int(row["version"]), ch=ch
        )
        return True

    if not run_connected(_go):
        print(f"  ✗ no live participant {participant}", file=sys.stderr)
        return 1
    print(f"  ✓ retired participant {participant}")
    return 0


def register_read_replica_cli(sub: Any) -> None:
    p = sub.add_parser("read-replica", help="Read-replicas: more Looms on the same ClickHouse.")
    ssub = p.add_subparsers(dest="read_replica_cmd", required=True)
    r = ssub.add_parser(
        "retire",
        help="Retire the provider participant of a read-replica whose process died "
        "without shutting down (its id is in that replica's /v1/health).",
    )
    r.add_argument("--participant", required=True, help="The participant id to retire")
    r.add_argument("--yes", action="store_true", help="Its process is confirmed stopped")
    r.set_defaults(func=_cmd_retire)
