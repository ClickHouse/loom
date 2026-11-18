"""Renew bounded Drive channel sets; ordinary source polling remains independent."""

from __future__ import annotations

import json
import logging
import time

from server.core.ch import set_query_tag
from server.knowledge.app.source_push import renew_drive, require_ready
from server.knowledge.store.source_push import drive_page
from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)


async def run_pass() -> dict[str, int]:
    set_query_tag("knowledge_source_push")
    await require_ready()
    try:
        after = json.loads(pass_context.resume_cursor() or '["", "", ""]')
    except ValueError:
        after = ["", "", ""]
    if not isinstance(after, list) or len(after) != 3 or not all(isinstance(s, str) for s in after):
        after = ["", "", ""]
    rows = await drive_page((after[0], after[1], after[2]))
    result = {"items_planned": len(rows), "items_done": 0, "failed": 0}
    # Leave one provider attempt and cleanup/status time before the supervisor cancels.
    deadline = (
        time.monotonic()
        + settings.knowledge_source_push_max_pass_seconds
        - settings.knowledge_source_push_provider_timeout_s
        - 2 * settings.knowledge_source_push_cleanup_timeout_s
    )
    with record_pass("knowledge_source_push") as rec:
        for reg in rows:
            pass_context.check_stop()
            if time.monotonic() >= deadline:
                break
            try:
                await renew_drive(reg)
                result["items_done"] += 1
            except Exception as e:  # noqa: BLE001 — one failed source must not starve renewal of others
                pass_context.note_failure(e, "channel renewal")
                log.warning("source push renewal failed: %s", type(e).__name__)
                result["failed"] += 1
            pass_context.set_cursor(json.dumps([reg.org, reg.namespace, reg.source_id]))
        if result["items_done"] + result["failed"] == len(rows):
            # A nonempty canonical cursor also resets a supervisor's older cursor.
            if len(rows) < 20:
                pass_context.set_cursor('["", "", ""]')
        rec.outcome = result
    return result
