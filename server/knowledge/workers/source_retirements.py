"""Finish queued source deletions even when source syncing or authentication is off."""

from server.knowledge.app.source_retirements import MAX_PASS_SECONDS as MAX_PASS_SECONDS
from server.knowledge.app.source_retirements import WORKER, run_once
from server.workers.status import record_pass


async def run_pass() -> None:
    with record_pass(WORKER, trace_pass=False):
        await run_once()
