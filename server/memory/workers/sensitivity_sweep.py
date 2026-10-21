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

A pass starts after the sort key the previous one stopped at (the worker's cursor in
`loom.worker_status`) and the next starts again at the first key once a pass reads the
last row, so selected rows the detector leaves unlabelled cannot fill every pass's pages
ahead of a later row.
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
    with record_pass("sensitivity_sweep") as rec:
        report = await sweep.scan(
            since_days=window,
            limit=limit,
            pages=int(settings.sensitivity_sweep_pages_per_pass),
            only_shapes=shapes or None,
            start_after=cursor_key(pass_context.resume_cursor()),
            ch=ch,
        )
        result = await sweep.apply(report.candidates, ch=ch)
        # after `apply`, so a pass that fails there is read again from the same key
        pass_context.set_cursor(cursor_text(report.resume_after))
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


def cursor_text(key: dict[str, Any] | None) -> str:
    """`key` as the pass's cursor: a JSON list of its sort key columns, `[]` for the first
    key (never empty, which would keep the previous cursor). A key longer than the cursor
    store holds keeps its leading columns and empties the rest, which sorts before it, so
    the next pass reads a few rows again and skips none."""
    if not key:
        return "[]"
    parts = [str(key[col]) for col in SORT_KEY]
    for kept in range(len(parts), 0, -1):
        text = json.dumps(parts[:kept] + [""] * (len(parts) - kept))
        if len(text) <= pass_context.MAX_CURSOR:
            return text
    return "[]"


def cursor_key(text: str) -> dict[str, Any] | None:
    """The sort key `cursor_text` wrote; None for `[]`, no cursor or one it did not write."""
    try:
        parts = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parts, list) or len(parts) != len(SORT_KEY):
        return None
    return dict(zip(SORT_KEY, map(str, parts), strict=True))


__all__ = ["cursor_key", "cursor_text", "run_pass"]
