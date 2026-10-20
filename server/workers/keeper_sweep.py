"""Delete the KeeperMap keys a process died holding (`server.core.leases.DEAD_KEYS`):
leases and claims an hour past their expiry, and graph projection slots a day old.
Leader-only like every worker."""

from __future__ import annotations

from server.core import leases
from server.workers.status import record_pass


async def run_pass() -> int:
    """The number of tables swept."""
    with record_pass("keeper_sweep") as rec:
        swept = await leases.sweep_dead_keys()
        rec.outcome = {"tables": swept}
    return swept
