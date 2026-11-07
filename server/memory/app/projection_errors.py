"""Projection-failure logging + source-id helpers for the memory extract path.

It imports the projection_failures service function-locally, so the extract
cluster stays acyclic.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, NamedTuple

from server.core.error_summary import error_summary
from server.core.errors import AuthInsufficientError, IdentityIncompleteError
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import (
    READ_SCOPE_KEY,
    REFUSED_READ_SCOPE,
    stored_read_scope,
)
from server.memory.domain.graph_quality import IntervalRepair

log = logging.getLogger(__name__)


class ProjectionScope(NamedTuple):
    """The identity a projection, its close or its repair runs as, where there is no
    request input: the writer's, or the row's. `user_id` goes on any failure recorded."""

    org: str
    namespace: str
    agent: str
    user_id: str


# The payload keys saying when a write's triples arrived (every failure), that they
# landed before their interval repair failed, which closes that repair had made, and
# what the write resolved its names to.
TRIPLES_ARRIVED_AT = "triples_arrived_at"
TRIPLES_INSERTED_AT = "triples_inserted_at"
TRIPLES_REPAIRS = "triples_repairs"
TRIPLES_ENTITY_MAP = "triples_entity_map"
# The id of the Undo or fallback whose markers a failed repair was repairing.
TRIPLES_RESTORE_ID = "triples_restore_id"

# The projection a triples repair that failed after its insert is recorded under. The
# build before replays every `entity_triples` failure by inserting the triples again,
# which would count them twice; it leaves a name it does not know active.
TRIPLES_REPAIR = "entity_triples_repair"
# A replay that inserted an `entity_triples` failure's triples and then failed to repair
# them records that in the failure itself, its entries and results moved under these
# keys. The build before inserts an `entity_triples` failure's `entries` again, and
# refuses one without them, leaving it active.
LANDED_ENTRIES = "triples_landed_entries"
LANDED_RESULTS = "triples_landed_results"


class TriplesInsertFailed(Exception):
    """A write's triples, dated at `arrived_at`, failed to insert. The replay inserts
    them dated at that arrival, not at its own clock. Some may have landed, so the
    replay names them by `entity_map`, as `TriplesRepairFailed` does."""

    def __init__(self, arrived_at: datetime, cause: BaseException) -> None:
        super().__init__(f"triples insert failed: {type(cause).__name__}: {cause}")
        self.arrived_at = arrived_at
        self.entity_map: dict[str, str] = {}


class TriplesRepairFailed(Exception):
    """A write's triples were inserted at `inserted_at` but their interval repair
    failed. `repairs` are the closes it had made. The replay finishes those, or
    repairs the triples as of `inserted_at`, without inserting them again.
    `entity_map` is what the write resolved its relation names to: the replay
    names the triples as they were inserted, not as names resolve by then."""

    def __init__(
        self,
        inserted_at: datetime,
        cause: BaseException,
        repairs: list[IntervalRepair] | None = None,
        *,
        restore_id: str = "",
    ) -> None:
        super().__init__(f"interval repair failed: {type(cause).__name__}: {cause}")
        self.inserted_at = inserted_at
        self.repairs = repairs or []
        self.entity_map: dict[str, str] = {}
        # Set when the triples were an Undo's or a fallback's markers (`restore_id`).
        self.restore_id = restore_id


def failed_projection(name: str, error: BaseException) -> str:
    """The projection to record a failed leg under: a triples write whose triples
    landed before its repair failed is `TRIPLES_REPAIR`."""
    if name == "entity_triples" and isinstance(error, TriplesRepairFailed):
        return TRIPLES_REPAIR
    return name


def messages_source_resource_id(input: Any) -> str:
    return (
        f"memory.set_from_messages:{getattr(input, 'session_id', '') or '-'}:"
        f"{getattr(input, 'run_id', '') or '-'}"
    )


async def record_input_projection_failure(
    input: Any,
    *,
    projection: str,
    error: BaseException | str,
    memory_keys: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    source_operation: str = "memory.set_from_messages",
    source_resource_id: str | None = None,
    ch: Any | None = None,
) -> None:
    try:
        from server.memory.app.projection_failures import record_projection_failure

        await record_projection_failure(
            org=input.org,
            namespace=input.namespace,
            agent=getattr(input, "agent", "") or "",
            projection=projection,
            source_operation=source_operation,
            source_resource_id=source_resource_id or messages_source_resource_id(input),
            memory_keys=memory_keys or [],
            error=error,
            payload=payload or {},
            user_id=str(getattr(input, "user_id", "") or ""),
            ch=ch,
        )
    except Exception as log_error:  # noqa: BLE001 — must not fail the write it describes
        # The summary, not the text: the payload it carried may hold the row's facts.
        log.warning(
            "projection failure log failed for %s (non-fatal): %s",
            projection,
            error_summary(log_error),
        )


def bulk_source_resource_id(input: Any) -> str:
    return (
        f"memory.bulk_set:{getattr(input, 'session_id', '') or '-'}:"
        f"{getattr(input, 'run_id', '') or '-'}"
    )


def bulk_projection_payload(
    input: Any, results: list[Any], *, error: BaseException | None = None
) -> dict[str, Any]:
    """What a replay of a bulk write's projection needs, the writer's read scope
    included: a replay runs outside the request, and retires only within it. A triples
    `error` records when the write arrived and what it resolved its names to; one after
    the triples landed also records the closes the repair had made, so the replay only
    finishes their repair."""
    entries: list[dict[str, Any]] = []
    for entry in list(getattr(input, "entries", []) or []):
        if hasattr(entry, "model_dump"):
            entries.append(entry.model_dump(mode="json"))
        elif isinstance(entry, dict):
            entries.append(dict(entry))
    result_rows: list[dict[str, Any]] = []
    for result in results:
        if hasattr(result, "model_dump"):
            result_rows.append(result.model_dump(mode="json"))
        elif isinstance(result, dict):
            result_rows.append(dict(result))
    payload = {
        "user_id": getattr(input, "user_id", "-") or "-",
        "session_id": getattr(input, "session_id", "-") or "-",
        "run_id": getattr(input, "run_id", "-") or "-",
        "entries": entries,
        "results": result_rows,
        READ_SCOPE_KEY: _writers_read_scope(),
    }
    if isinstance(error, TriplesInsertFailed):
        payload[TRIPLES_ARRIVED_AT] = error.arrived_at.isoformat()
        payload[TRIPLES_ENTITY_MAP] = dict(error.entity_map)
    if isinstance(error, TriplesRepairFailed):
        payload.update(repair_failure_payload(error))
    return payload


def repair_failure_payload(error: TriplesRepairFailed) -> dict[str, Any]:
    """The payload keys that carry a failed repair to its replay, as JSON."""
    return {
        TRIPLES_ARRIVED_AT: error.inserted_at.isoformat(),
        TRIPLES_INSERTED_AT: error.inserted_at.isoformat(),
        TRIPLES_REPAIRS: [
            {k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(r).items()}
            for r in error.repairs
        ],
        TRIPLES_ENTITY_MAP: dict(error.entity_map),
    }


def restated_repair_payload(error: BaseException) -> dict[str, Any]:
    """The payload keys that carry a failed repair of an Undo's or a fallback's markers
    to its replay: their id, when they were stated and the closes made. Empty for any
    other error."""
    if not isinstance(error, TriplesRepairFailed) or not error.restore_id:
        return {}
    return {**repair_failure_payload(error), TRIPLES_RESTORE_ID: error.restore_id}


def _writers_read_scope() -> dict[str, Any]:
    try:
        return stored_read_scope(request_scope())
    except (AuthInsufficientError, IdentityIncompleteError):
        # The request's scope was refused: store one the replay refuses too.
        return {"scope": REFUSED_READ_SCOPE}
