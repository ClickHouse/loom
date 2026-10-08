"""What a closing run writes back into memory: usage events on the rows it
relied on, one procedure run per procedure it followed, and its reflection
with the edges to what the reflection rests on. `run.complete` decides what
to write and calls these with plain values."""

from __future__ import annotations

from typing import Any

import server.memory.store as memory_repo
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_graph import MemoryLinkInput
from server.memory.api.models_procedure import ProcedureRecordRunInput
from server.memory.app import ops as memory_ops
from server.memory.app import procedure as procedure_handlers
from server.memory.app import write as write_mod


async def record_usage(
    *,
    memory_id: str,
    org: str,
    namespace: str,
    event_type: str,
    event_count: int,
    agent: str,
    ch: Any,
) -> None:
    await memory_repo.record_usage_event(
        memory_id=memory_id,
        org=org,
        namespace=namespace,
        event_type=event_type,
        event_count=event_count,
        agent=agent,
        ch=ch,
    )


async def record_procedure_run(
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    ok: bool,
    note: str,
    duration_ms: int,
) -> None:
    """One row in the procedure's run ledger. The path is empty and the step
    and token counts zero: a run report knows how the run ended and its wall
    clock, not which steps were visited."""
    await procedure_handlers.handle_procedure_record_run(
        ProcedureRecordRunInput(
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
            memory_key=memory_key,
            path=[],
            ok=ok,
            note=note,
            steps_taken=0,
            duration_ms=duration_ms,
        )
    )


async def write_reflection(
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    run_id: str,
    memory_key: str,
    value: dict[str, Any],
    tags: list[str],
    confidence: float,
    ttl_seconds: int | None,
) -> None:
    """The run's `reflection` memory, through the normal memory.set path
    (blacklist, embedding, provenance) under the run's own identity."""
    await write_mod.handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            memory_key=memory_key,
            memory_type="reflection",
            value=value,
            tags=tags,
            confidence=confidence,
            source_trace=f"run:{run_id}",
            ttl_seconds=ttl_seconds,
        )
    )


async def link_derived_from(
    *,
    org: str,
    namespace: str,
    from_key: str,
    to_key: str,
    explanation: str,
    confidence: float,
    run_id: str,
) -> None:
    await memory_ops.handle_memory_link(
        MemoryLinkInput(
            org=org,
            namespace=namespace,
            from_key=from_key,
            to_key=to_key,
            relation="derived_from",
            explanation=explanation,
            confidence=confidence,
            run_id=run_id,
        )
    )
