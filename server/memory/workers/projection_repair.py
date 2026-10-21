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
from server.llm.identity import IdentityUnread, reads_writer
from server.memory.domain.trust import subject_of
from server.settings import settings
from server.workers.holds import UNREAD_HOLD_S, Held, hold_seconds
from server.workers.status import record_pass

log = logging.getLogger(__name__)
_HELD: Held[str] = Held()


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
    if (
        failure.projection not in {"memory_chunks", "entity_vectors"}
        or not failure.memory_keys
        or not reads_writer()
    ):
        return ""  # nothing names it but identity headers
    import server.memory.store as memory_repo
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    keys = [(str(failure.namespace), str(k)) for k in failure.memory_keys]
    found = await memory_repo.actors_by_key(str(failure.org), keys, scope=WHOLE_NAMESPACE)
    wanted = {c: str(failure.payload.get(c) or "-") for c in ("user_id", "session_id")}
    if failure.projection == "memory_chunks":
        from server.memory.app.projection_failures import _session_from_source_resource_id

        wanted["session_id"] = _session_from_source_resource_id(failure.source_resource_id)
        wanted.pop("user_id")
    subjects = set()
    for key in keys:
        for head in found.get(key, []):
            if head["agent"] != failure.agent:
                continue
            if any(head[c] != value for c, value in wanted.items()):
                continue
            if subject := subject_of(head["actor"]):
                subjects.add(subject)
    if len(subjects) > 1:
        raise IdentityUnread("the projection's memories name more than one principal")
    return next(iter(subjects), "")


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
            exclude_ids=_HELD.current(),
        )
        result.scanned = len(failures)
        for failure in failures:
            try:
                with customer.for_org(
                    str(failure.org),
                    namespace=str(failure.namespace),
                    subject=await _writer(failure),
                    user_id=str(failure.payload.get("user_id") or ""),
                ):
                    retry = await retry_projection_failure(failure)
            except customer.GROUP_HOLDS as e:
                _HELD.hold([failure.failure_id], hold_seconds(e))
                continue
            if retry.reason == "model_calls_held":
                _HELD.hold([failure.failure_id], UNREAD_HOLD_S)
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
