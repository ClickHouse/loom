"""The `expire_reason` prefix a blacklist sweep stamps on its tombstones.

The write fence (server/memory/app/write_fence.py) tells a sweep's tombstone from a
delete's by it, so no caller may write it: an `expire_reason` a caller sends with the
prefix is refused.
"""

from __future__ import annotations

SWEEP_REASON_PREFIX = "blacklist.sweep:"


def caller_reason(reason: str) -> str:
    """`reason` when a caller may store it as an `expire_reason`; raises otherwise."""
    if reason.startswith(SWEEP_REASON_PREFIX):
        raise ValueError(f"an expire_reason may not start with {SWEEP_REASON_PREFIX!r}")
    return reason
