"""Runs ingestion jobs (`server/knowledge/app/ingest_run.py`).

A pass runs one due job, pending ones first and namespaces in turn
(`ingestions.due`), under the job's own lease:
extraction and embedding are the expensive parts, and one source at a time
keeps a large file from starving the embedder's other callers, and gives the
job the whole pass deadline (`knowledge_ingest_max_pass_seconds`). A job
another process holds is skipped for the next due one. A job a crashed or
cancelled pass left `processing` is due again once no worker has updated it for
its lease's TTL, and runs from its first stage (the artifact is
content-addressed, vectors are reused, and a publish its own earlier run made is
found and not repeated); each such run counts toward its attempts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.knowledge.app import ingest_run
from server.knowledge.store import ingestions
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Due jobs one pass reads to find one it can take.
_CANDIDATES = 8  # not a setting: only jobs another process holds are passed over


@dataclass(frozen=True)
class IngestResult:
    state: str | None  # the state the pass's job ended in; None when it ran none


async def run_pass() -> IngestResult:
    state = None
    with record_pass("knowledge_ingest") as rec:
        jobs = await ingestions.due(_CANDIDATES, stale_after_s=ingest_run.JOB_LEASE_S)
        for job in jobs:
            pass_context.check_stop()
            state = await ingest_run.take_and_run(job)
            if state is not None:
                break
        rec.outcome = {"ran": int(state is not None), "state": state or ""}
    if state is None:
        pass_context.mark_idle()
    return IngestResult(state=state)
