"""Projection repair worker.

Memory writes are intentionally fail-open for secondary projections: source
evidence links, raw chunks, and graph side tables should not make the primary
belief write fail. This worker gives those soft failures a bounded replay path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.llm import customer
from server.llm.identity import reads_writer
from server.memory.domain.trust import subject_of
from server.settings import settings
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectionRepairConfig:
    # Read when a pass builds its config, not at import.
    max_failures_per_pass: int = field(
        default_factory=lambda: settings.projection_repair_max_failures_per_pass
    )


@dataclass
class ProjectionRepairResult:
    scanned: int = 0
    retried: int = 0
    resolved: int = 0
    unsupported: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)


async def _writer(failure: Any) -> str:
    """Whom a failure's replay is for: the principal that wrote its memories ('' when
    none recorded one, and the failure's namespace decides)."""
    if not failure.memory_keys or not reads_writer():
        return ""  # nothing names it but identity headers
    import server.memory.store as memory_repo
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    keys = [(str(failure.namespace), str(k)) for k in failure.memory_keys]
    found = await memory_repo.actors_by_key(str(failure.org), keys, scope=WHOLE_NAMESPACE)
    for key in keys:
        for head in found.get(key, []):
            if subject := subject_of(head["actor"]):
                return subject
    return ""


async def run_pass(
    cfg: ProjectionRepairConfig | None = None,
) -> ProjectionRepairResult:
    cfg = cfg or ProjectionRepairConfig()
    result = ProjectionRepairResult()
    with record_pass("projection_repair") as rec:
        from server.memory.app.projection_failures import (
            retry_projection_failure,
            scan_active_projection_failures,
        )

        failures = await scan_active_projection_failures(
            limit=max(1, int(cfg.max_failures_per_pass)),
            exclude_orgs=await customer.switched_off_orgs(),  # theirs wait, taking no slot
        )
        result.scanned = len(failures)
        for failure in failures:
            with customer.for_org(
                str(failure.org),
                namespace=str(failure.namespace),
                subject=await _writer(failure),
            ):
                retry = await retry_projection_failure(failure)
            if retry.retried:
                result.retried += 1
            if retry.resolved:
                result.resolved += 1
            if retry.reason == "unsupported_projection":
                result.unsupported += 1
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
            "failed": len(result.failed),
        }

    log.info(
        "projection repair pass: scanned=%d retried=%d resolved=%d unsupported=%d failed=%d",
        result.scanned,
        result.retried,
        result.resolved,
        result.unsupported,
        len(result.failed),
    )
    return result


__all__ = [
    "ProjectionRepairConfig",
    "ProjectionRepairResult",
    "run_pass",
]
