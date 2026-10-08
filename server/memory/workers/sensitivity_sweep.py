"""Daily sensitivity sweep.

`resolve_sensitivity` runs on `memory.set` and the extractor's entries
only. `observe.record`, `copy_to`, `namespace.restore` and the imports write
`sensitivity = ''` whatever the value holds, so a credential or a contact
detail that entered by one of those paths is embedded and searchable. This
pass runs the detector late over rows created in the trailing window
(`sensitivity_sweep_window_days`, 2) and labels what it finds the way the
write path would have — a new version per row, the `high` rows' embedding
rows tombstoned. Bounded per pass and limited to the shapes in
`sensitivity_sweep_shapes` (credential and email by default — the phone
shape matches run ids and integer limits); the historical
backlog is the operator's `loom sensitivity-sweep --apply`, not this worker's.
"""

from __future__ import annotations

import logging
from typing import Any

from server.core.repo_base import _CHClient
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
        report = await sweep.scan(since_days=window, limit=limit, only_shapes=shapes or None, ch=ch)
        result = await sweep.apply(report.candidates, ch=ch)
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


__all__ = ["run_pass"]
