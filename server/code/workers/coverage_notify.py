"""Coverage-watch delivery.

One pass: the pending `loom.code_coverage_watches` rows, grouped by
namespace with one `code_index_state` read per pass, each checked with the
`commit_coverage` logic `code.index_status` answers with; a covered commit
gets one `code.coverage.ready` event on the watcher's inbox and the watch's
delivered version (`server.code.app.watches`). The supervisor
runs the pass in the lease-holding process only.
"""

from __future__ import annotations

import logging

from server.code.app.watches import NotifyResult, notify_once
from server.workers import pass_context

log = logging.getLogger(__name__)


async def run_pass() -> NotifyResult:
    result = await notify_once()
    if result.pending == 0:
        pass_context.mark_idle()
    elif result.delivered:
        log.info(
            "code_coverage_notify: pending=%d delivered=%d emitted=%d",
            result.pending,
            result.delivered,
            result.emitted,
        )
    return result
