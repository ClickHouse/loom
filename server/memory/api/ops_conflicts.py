"""Operations over the wire models in `server.memory.api.models_conflicts`."""

from __future__ import annotations

from server.memory.api.models_conflicts import (
    ConflictsAllowlistListInput,
    ConflictsAllowlistListOutput,
    ConflictsAllowlistRemoveInput,
    ConflictsAllowlistRemoveOutput,
    ConflictsListInput,
    ConflictsListOutput,
    ConflictsProposeRevisionInput,
    ConflictsProposeRevisionOutput,
    ConflictsResolveInput,
    ConflictsResolveOutput,
)
from server.memory.app.conflicts_ops import (
    handle_conflicts_allowlist_list,
    handle_conflicts_allowlist_remove,
    handle_conflicts_list,
    handle_conflicts_propose_revision,
    handle_conflicts_resolve,
)
from server.operations.base import Operation, Role

conflicts_list = Operation(
    name="conflicts.list",
    input_model=ConflictsListInput,
    output_model=ConflictsListOutput,
    handler=handle_conflicts_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

conflicts_resolve = Operation(
    name="conflicts.resolve",
    input_model=ConflictsResolveInput,
    output_model=ConflictsResolveOutput,
    handler=handle_conflicts_resolve,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)


conflicts_propose_revision = Operation(
    name="conflicts.propose_revision",
    maturity="experimental",
    input_model=ConflictsProposeRevisionInput,
    output_model=ConflictsProposeRevisionOutput,
    handler=handle_conflicts_propose_revision,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)


conflicts_allowlist_list = Operation(
    name="conflicts.allowlist_list",
    maturity="experimental",
    input_model=ConflictsAllowlistListInput,
    output_model=ConflictsAllowlistListOutput,
    handler=handle_conflicts_allowlist_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

conflicts_allowlist_remove = Operation(
    name="conflicts.allowlist_remove",
    maturity="experimental",
    input_model=ConflictsAllowlistRemoveInput,
    output_model=ConflictsAllowlistRemoveOutput,
    handler=handle_conflicts_allowlist_remove,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)


OPERATIONS: list[Operation] = [
    conflicts_list,
    conflicts_propose_revision,
    conflicts_resolve,
    conflicts_allowlist_list,
    conflicts_allowlist_remove,
]
