"""`loom participant retire`: the manual recovery for a Loom process that died
without shutting down.

A participant retires only after permanent guarded shutdown or verifiable exact
physical death on a trusted host, with its cross-replica queries settled.
Heartbeat age and an operator affirmation are not evidence of death.
"""

from __future__ import annotations

import argparse
import sys
from time import monotonic
from typing import Any

from server.cli._ch import writes


def _cmd_retire(args: argparse.Namespace) -> int:
    """Verify exact process death and retire the participant after its queries settle."""
    from uuid import UUID

    from server.cli._ch import run_connected

    try:
        participant = UUID(args.participant)
    except ValueError:
        print(f"  ✗ {args.participant!r} is not a participant id", file=sys.stderr)
        return 2

    async def _go(ch: Any) -> str:
        from server.insights.app.provider_barrier import SHUTDOWN_RETIRE_S, retire_participant

        end = monotonic() + SHUTDOWN_RETIRE_S
        return await retire_participant(participant, ch=ch, end=end)

    outcome = run_connected(_go, org_free=writes("a participant row of this cell"))
    if outcome == "absent":
        print(f"  ✗ no live participant {participant}", file=sys.stderr)
        return 1
    if outcome == "live":
        print(
            f"  ✗ participant {participant} has no verifiable exact physical-death proof "
            "or its cross-replica queries have not settled; run on its trusted host after "
            "the exact process stops. Expiry and Kubernetes object deletion are not proof",
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
        help="Verify exact physical death on this trusted host and retire its participant "
        "after all queries settle (the id is participant_id in /v1/health).",
    )
    r.add_argument("--participant", required=True, help="The participant id to retire")
    r.set_defaults(func=_cmd_retire)
