"""Operations over the wire models in `server.memory.api.models_lock`."""

from __future__ import annotations

from server.memory.api.models_lock import (
    MemoryLockInput,
    MemoryLockOutput,
    MemoryUnlockInput,
    MemoryUnlockOutput,
)
from server.memory.app.lock import handle_memory_lock, handle_memory_unlock
from server.operations.base import Operation, Role

memory_lock = Operation(
    name="memory.lock",
    maturity="experimental",
    input_model=MemoryLockInput,
    output_model=MemoryLockOutput,
    handler=handle_memory_lock,
    idempotent=False,  # a re-lock with a new reason is a new version row
    budget_p99_ms=40,
    required_role=Role.WRITER,
    # A lock is a statement about what may happen to a fact — auditable.
    audit=True,
)

memory_unlock = Operation(
    name="memory.unlock",
    maturity="experimental",
    input_model=MemoryUnlockInput,
    output_model=MemoryUnlockOutput,
    handler=handle_memory_unlock,
    idempotent=True,
    budget_p99_ms=40,
    # Loosening is the privileged direction, so it needs ADMIN.
    required_role=Role.ADMIN,
    audit=True,
)
