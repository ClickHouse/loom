"""Pulls each registered document source's changed files into its repository's
documents (`server.knowledge.app.source_sync`)."""

from __future__ import annotations

import logging

from server.knowledge.app.source_sync import WORKER, PassResult, run_sync
from server.workers.status import log_pass_errors, record_pass

logger = logging.getLogger("loom.workers.knowledge_source_sync")


async def run_pass() -> PassResult:
    with record_pass(WORKER) as rec:
        result = await run_sync()
        log_pass_errors(logger, WORKER, result.errors)
        rec.outcome = outcome(result)
    return result


def outcome(result: PassResult) -> dict[str, int | bool]:
    """The pass's counts as `loom.worker_status` reads them."""
    return {
        "items_planned": result.sources,
        "items_done": result.synced,
        "rows_written": result.changed + result.removed + result.retired,
        "changed": result.changed,
        "removed": result.removed + result.retired,
        "unconverted": result.unconverted,
        "skipped": result.skipped,
        "failed": result.failed,
        "cut": result.cut,
    }
