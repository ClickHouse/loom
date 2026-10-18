"""Projection repair worker.

Memory writes are intentionally fail-open for secondary projections: source
evidence links, raw chunks, and graph side tables should not make the primary
belief write fail. This worker gives those soft failures a bounded replay path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from server.settings import settings
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# STATUS_MIRROR intents read at a time, before and apart from the other failures: one per
# judged retirement, nearly all no-ops, so a burst of them never delays a real failure.
STATUS_MIRROR_BATCH = 300


@dataclass(frozen=True)
class ProjectionRepairConfig:
    # Read when a pass builds its config, not at import.
    max_failures_per_pass: int = field(
        default_factory=lambda: settings.projection_repair_max_failures_per_pass
    )
    status_mirror_seconds: float = field(
        default_factory=lambda: settings.projection_repair_status_mirror_seconds
    )


@dataclass
class ProjectionRepairResult:
    scanned: int = 0
    retried: int = 0
    resolved: int = 0
    unsupported: int = 0
    # Given up on: a failure no retry can do its work for (its reason says why).
    abandoned: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)


async def run_pass(
    cfg: ProjectionRepairConfig | None = None,
) -> ProjectionRepairResult:
    cfg = cfg or ProjectionRepairConfig()
    result = ProjectionRepairResult()
    with record_pass("projection_repair") as rec:
        from server.memory.app.projection_failures import (
            Retried,
            mark_retried,
            retry_each,
            retry_failures,
            scan_active_projection_failures,
        )

        mirrored: list[Retried] = []
        until = time.monotonic() + float(cfg.status_mirror_seconds)
        try:
            after = None
            while True:
                batch = await scan_active_projection_failures(
                    limit=STATUS_MIRROR_BATCH, status_mirror=True, after=after
                )
                # The first mirror of a pass always runs: a zero budget still moves the queue.
                async for retried in retry_each(batch, until=until, first=not mirrored):
                    mirrored.append(retried)
                if len(batch) < STATUS_MIRROR_BATCH or time.monotonic() >= until:
                    break
                # Past the batch's last row: a row left pending, or retried and not marked
                # yet, is not read again this pass.
                after = batch[-1]
        finally:
            # One mark per org for the whole loop; a pass cut short marks what it did.
            await mark_retried(mirrored)
        retries = [r for _, r in mirrored]
        others = await scan_active_projection_failures(
            limit=max(1, int(cfg.max_failures_per_pass)), status_mirror=False
        )
        retries += await retry_failures(others)
        result.scanned = len(retries)
        for retry in retries:
            if retry.retried:
                result.retried += 1
            if retry.resolved:
                result.resolved += 1
            if retry.reason == "unsupported_projection":
                result.unsupported += 1
            if retry.abandoned:
                result.abandoned += 1
            if retry.error:
                result.failed.append(
                    {
                        "failure_id": retry.failure_id,
                        "projection": retry.projection,
                        "error": retry.error[:512],
                    }
                )
        rec.outcome = {
            "scanned": result.scanned,
            "retried": result.retried,
            "resolved": result.resolved,
            "unsupported": result.unsupported,
            "abandoned": result.abandoned,
            "failed": len(result.failed),
        }

    log.info(
        "projection repair pass: scanned=%d retried=%d resolved=%d unsupported=%d"
        " abandoned=%d failed=%d",
        result.scanned,
        result.retried,
        result.resolved,
        result.unsupported,
        result.abandoned,
        len(result.failed),
    )
    return result


__all__ = [
    "ProjectionRepairConfig",
    "ProjectionRepairResult",
    "run_pass",
]
