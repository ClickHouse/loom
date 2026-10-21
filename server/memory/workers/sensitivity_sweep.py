"""Daily sensitivity sweep.

`resolve_sensitivity` runs on `memory.set`, `memory.bulk_set`, `memory.update`
and the extractor's entries only. `observe.record`, `copy_to`, `namespace.restore`
and the imports write `sensitivity = ''` whatever the value holds, so a credential or a contact
detail that entered by one of those paths is embedded and searchable. This
pass runs the detector late over rows created in the trailing window
(`sensitivity_sweep_window_days`, 2) and labels what it finds the way the
write path would have — a new version per row, the `high` rows' embedding
rows tombstoned. Bounded per pass and limited to the shapes in
`sensitivity_sweep_shapes` (credential and email by default — the phone
shape matches run ids and integer limits); the historical
backlog is the operator's `loom sensitivity-sweep --apply`, not this worker's.

A pass starts at the agent the previous one stopped in (the worker's cursor in
`loom.worker_status`), past it when the previous pass started and stopped in that one
agent, and the next starts again at the first key once a pass reads the last row. So
selected rows the detector leaves unlabelled in one agent cannot fill every pass's pages
ahead of another agent's rows. The cursor is `(org, namespace, agent)` and no more: a user
id or a memory key in `loom.worker_status` would outlive that user's erasure.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from server.core.repo_base import _CHClient
from server.memory.store.sensitivity_sweep_sql import SORT_KEY
from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass

log = logging.getLogger(__name__)


async def run_pass(ch: _CHClient | None = None) -> dict[str, Any]:
    from server.core.repo_base import _default_client
    from server.memory.app import sensitivity_sweep as sweep
    from server.settings import settings

    ch = ch or _default_client()
    window = max(1, int(settings.sensitivity_sweep_window_days))
    limit = int(settings.sensitivity_sweep_max_rows_per_pass)
    shapes = [x.strip() for x in str(settings.sensitivity_sweep_shapes).split(",") if x.strip()]
    start = cursor_key(pass_context.resume_cursor())
    with record_pass("sensitivity_sweep") as rec:
        report = await sweep.scan(
            since_days=window,
            limit=limit,
            pages=int(settings.sensitivity_sweep_pages_per_pass),
            only_shapes=shapes or None,
            start_after=start,
            ch=ch,
        )
        result = await sweep.apply(report.candidates, ch=ch)
        # after `apply`, so a pass that fails there is read again from the same agent
        pass_context.set_cursor(cursor_text(report.resume_after, started_at=start))
        outcome = {"scanned": report.scanned, "truncated": report.truncated, **result.as_dict()}
        rec.outcome = outcome
        if result.relabelled or result.errors:
            log.info(
                "sensitivity_sweep: %d relabelled (%d high, %d medium), %d embedding rows "
                "tombstoned, %d tagged, %d errors",
                result.relabelled,
                result.high,
                result.medium,
                result.embeddings_tombstoned,
                result.embeddings_tagged,
                len(result.errors),
            )
        log_pass_errors(log, "sensitivity_sweep", result.errors)
    return outcome


# The sort key columns the cursor keeps. The rest name a user and a memory, which erasure
# does not clear from `loom.worker_status`.
CURSOR_KEY = SORT_KEY[:3]


def cursor_text(key: dict[str, Any] | None, *, started_at: dict[str, Any] | None = None) -> str:
    """The agent of `key` as the pass's cursor: a JSON list of its org, namespace and agent,
    `[]` for the first key (never empty, which would keep the previous cursor). When the pass
    started in that same agent (`started_at`), the cursor is the next agent instead, so the
    next pass does not read the same page again. A key longer than the cursor store holds
    keeps its leading columns and empties the rest, which sorts before it."""
    if not key:
        return "[]"
    parts = [str(key[col]) for col in CURSOR_KEY]
    if started_at and parts == [started_at[col] for col in CURSOR_KEY]:
        # sorts after every key in this agent and before the next agent, as no agent name
        # holds a control character (a NUL would not pass as a query parameter)
        parts[-1] += "\x01"
    for kept in range(len(parts), 0, -1):
        text = json.dumps(parts[:kept] + [""] * (len(parts) - kept))
        if len(text) <= pass_context.MAX_CURSOR:
            return text
    return "[]"


def cursor_key(text: str) -> dict[str, Any] | None:
    """The sort key a pass starts after: the agent `cursor_text` wrote, with the user,
    session and memory key empty, which sorts before every key in that agent that has a
    memory key. None for `[]`, no cursor or one it did not write."""
    try:
        parts = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parts, list) or len(parts) != len(CURSOR_KEY):
        return None
    key = dict.fromkeys(SORT_KEY, "")
    key.update(zip(CURSOR_KEY, map(str, parts), strict=True))
    return key


__all__ = ["CURSOR_KEY", "cursor_key", "cursor_text", "run_pass"]
