"""Pure participant versions and the existing cell-control refusal envelope."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from server.core.cell_ch import Refusal, Refused


def refused(reason: str) -> Refused:
    return Refused([Refusal("cell control", reason)])


def latest(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    # The latest winner cannot hide a conflicting older version from another replica.
    seen: dict[tuple[str, int], tuple[str, str]] = {}
    for row in rows:
        try:
            identity = str(UUID(str(row["participant_id"])))
            version = row["version"]
            state, proof = row["state"], row["host_proof"]
        except (KeyError, TypeError, ValueError):
            raise refused("participant census contains an unreadable row") from None
        if (
            type(version) is not int
            or version <= 0
            or state not in ("running", "drained", "retired")
            or not isinstance(proof, str)
        ):
            raise refused("participant census contains an unreadable row")
        previous = out.get(identity)
        observed = seen.get((identity, version))
        if observed is not None and observed != (state, proof):
            raise refused(f"participant {identity} has conflicting equal versions")
        seen[(identity, version)] = (state, proof)
        if previous is None or int(previous["version"]) < version:
            out[identity] = dict(row)
    return out


def bounded_participant(row: dict[str, Any]) -> dict[str, Any]:
    """The bounded mode retains the actual UUID, birth, state and integral version."""
    from server.core.provider_process import certified

    try:
        identity = str(UUID(row["participant_id"]))
        proof = row["host_proof"]
        if identity != row["participant_id"] or not isinstance(proof, str):
            raise ValueError
        if len(proof.encode()) > 2048:
            raise ValueError
        certified(proof)
        if type(row.get("version")) is not int or not 0 < row["version"] < 2**64:
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise refused("participant census contains an unreadable birth") from None
    # The existing version/state/conflict policy remains the sole selector.
    return latest([row])[identity]
