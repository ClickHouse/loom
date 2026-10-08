"""Projection-failure logging + source-id helpers for the memory extract path.

It imports the projection_failures service function-locally, so the extract
cluster stays acyclic.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


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
        )
    except Exception as log_error:  # noqa: BLE001 — must not fail the write it describes
        log.warning(
            "projection failure log failed for %s (non-fatal): %s",
            projection,
            log_error,
        )


def bulk_source_resource_id(input: Any) -> str:
    return (
        f"memory.bulk_set:{getattr(input, 'session_id', '') or '-'}:"
        f"{getattr(input, 'run_id', '') or '-'}"
    )


def bulk_projection_payload(input: Any, results: list[Any]) -> dict[str, Any]:
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
    return {
        "user_id": getattr(input, "user_id", "-") or "-",
        "session_id": getattr(input, "session_id", "-") or "-",
        "run_id": getattr(input, "run_id", "-") or "-",
        "entries": entries,
        "results": result_rows,
    }
