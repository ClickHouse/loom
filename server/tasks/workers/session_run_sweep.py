"""MCP session run sweeper.

One pass completes the implicit MCP session runs that no call of their token
reached within `mcp_session_run_idle_seconds` (`close_idle_session_runs`).
It reads the ledger only, so a run closes whichever web process served it,
and after a restart that dropped every process's cache.
"""

from __future__ import annotations

import logging

from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 60


async def run_pass() -> int:
    from server.settings import settings
    from server.tasks.app.run import close_idle_session_runs

    with record_pass("mcp_session_run_sweep") as rec:
        closed = await close_idle_session_runs(settings.mcp_session_run_idle_seconds)
        rec.outcome = {"closed": closed}
    if closed:
        log.info("mcp_session_run_sweep: closed %d idle session runs", closed)
    else:
        pass_context.mark_idle()
    return closed
