"""Operations over the wire models in `server.memory.api.models_crud`."""

from __future__ import annotations

from server.memory.api.models_apply import MemoryApplyInput, MemoryApplyOutput
from server.memory.api.models_crud import (
    MemoryAutoRetirementsInput,
    MemoryAutoRetirementsOutput,
    MemoryClearInput,
    MemoryClearOutput,
    MemoryDeleteInput,
    MemoryDeleteOutput,
    MemoryDeleteVersionInput,
    MemoryDeleteVersionOutput,
    MemoryGetByIdInput,
    MemoryGetByIdOutput,
    MemoryGetInput,
    MemoryGetOutput,
    MemoryKeysInput,
    MemoryKeysOutput,
    MemoryListInput,
    MemoryListOutput,
    MemoryRedactInput,
    MemoryRedactOutput,
    MemoryRestoreInput,
    MemoryRestoreOutput,
    MemorySetInput,
    MemorySetOutput,
    MemoryStatusesInput,
    MemoryStatusesOutput,
    MemorySweepExpiredInput,
    MemorySweepExpiredOutput,
    MemoryTouchInput,
    MemoryTouchOutput,
    MemoryUnpinInput,
    MemoryUnpinOutput,
    MemoryUpdateByIdInput,
    MemoryUpdateByIdOutput,
    MemoryUpdateInput,
    MemoryUpdateOutput,
    MemoryVerifyInput,
    MemoryVerifyOutput,
)
from server.memory.app.apply_ops import (
    handle_memory_apply,
    handle_memory_sweep_expired,
)
from server.memory.app.auto_resolution import handle_memory_auto_retirements
from server.memory.app.curation import handle_memory_redact
from server.memory.app.handlers import (
    handle_memory_clear,
    handle_memory_delete,
    handle_memory_delete_version,
    handle_memory_get,
    handle_memory_get_by_id,
    handle_memory_pin,
    handle_memory_touch,
    handle_memory_unpin,
)
from server.memory.app.query import handle_memory_keys, handle_memory_list, handle_memory_statuses
from server.memory.app.restore import handle_memory_restore
from server.memory.app.update import handle_memory_update, handle_memory_update_by_id
from server.memory.app.verify import handle_memory_verify
from server.memory.app.write import handle_memory_set
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_get = Operation(
    name="memory.get",
    input_model=MemoryGetInput,
    output_model=MemoryGetOutput,
    handler=handle_memory_get,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_set = Operation(
    name="memory.set",
    input_model=MemorySetInput,
    output_model=MemorySetOutput,
    handler=handle_memory_set,
    idempotent=True,  # idempotent on (identity tuple, memory_key)
    required_role=Role.WRITER,
    audit=False,
)

memory_pin = Operation(
    name="memory.pin",
    maturity="experimental",
    input_model=MemorySetInput,  # reuses set's input shape
    output_model=MemorySetOutput,
    handler=handle_memory_pin,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)

memory_unpin = Operation(
    name="memory.unpin",
    maturity="experimental",
    input_model=MemoryUnpinInput,
    output_model=MemoryUnpinOutput,
    handler=handle_memory_unpin,
    idempotent=True,
    # Removing a write-time invariant is the privileged direction, like
    # memory.unlock.
    required_role=Role.ADMIN,
    audit=True,
)

memory_delete = Operation(
    name="memory.delete",
    input_model=MemoryDeleteInput,
    output_model=MemoryDeleteOutput,
    handler=handle_memory_delete,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,  # delete is auditable
)

memory_delete_version = Operation(
    name="memory.delete_version",
    maturity="experimental",
    input_model=MemoryDeleteVersionInput,
    output_model=MemoryDeleteVersionOutput,
    handler=handle_memory_delete_version,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

memory_restore = Operation(
    name="memory.restore",
    input_model=MemoryRestoreInput,
    output_model=MemoryRestoreOutput,
    handler=handle_memory_restore,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

memory_clear = Operation(
    name="memory.clear",
    input_model=MemoryClearInput,
    output_model=MemoryClearOutput,
    handler=handle_memory_clear,
    idempotent=True,  # second clear on already-tombstoned is a no-op
    required_role=Role.ADMIN,
    audit=True,
)

memory_list = Operation(
    name="memory.list",
    input_model=MemoryListInput,
    output_model=MemoryListOutput,
    handler=handle_memory_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_keys = Operation(
    name="memory.keys",
    maturity="experimental",
    input_model=MemoryKeysInput,
    output_model=MemoryKeysOutput,
    handler=handle_memory_keys,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_touch = Operation(
    name="memory.touch",
    maturity="experimental",
    input_model=MemoryTouchInput,
    output_model=MemoryTouchOutput,
    handler=handle_memory_touch,
    idempotent=False,  # writes a new version each call
    required_role=Role.WRITER,
    audit=False,
)


memory_apply = Operation(
    name="memory.apply",
    maturity="experimental",
    input_model=MemoryApplyInput,
    output_model=MemoryApplyOutput,
    handler=handle_memory_apply,
    idempotent=False,
    required_role=Role.WRITER,
    # A wide write is exactly what an audit wants to see: the keys it named and its
    # reason's hash.
    audit=True,
)


memory_sweep_expired = Operation(
    name="memory.sweep_expired",
    maturity="experimental",
    input_model=MemorySweepExpiredInput,
    output_model=MemorySweepExpiredOutput,
    handler=handle_memory_sweep_expired,
    idempotent=True,  # a second pass finds nothing left to do
    required_role=Role.ADMIN,
    audit=True,
)

memory_get_by_id = Operation(
    name="memory.get_by_id",
    input_model=MemoryGetByIdInput,
    output_model=MemoryGetByIdOutput,
    handler=handle_memory_get_by_id,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
    row_scoped=True,  # The handler authorizes the row's namespace
)

memory_update_by_id = Operation(
    name="memory.update_by_id",
    input_model=MemoryUpdateByIdInput,
    output_model=MemoryUpdateByIdOutput,
    handler=handle_memory_update_by_id,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,  # The handler writes the hash-only row, in the row's namespace
    row_scoped=True,  # The handler authorizes the row's namespace
)

memory_update = Operation(
    name="memory.update",
    input_model=MemoryUpdateInput,
    output_model=MemoryUpdateOutput,
    handler=handle_memory_update,
    idempotent=False,
    required_role=Role.WRITER,
    audit=False,  # The handler writes the hash-only row
)

memory_redact = Operation(
    name="memory.redact",
    maturity="experimental",
    input_model=MemoryRedactInput,
    output_model=MemoryRedactOutput,
    handler=handle_memory_redact,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
    row_scoped=True,  # The handler authorizes the row's namespace
)


memory_verify = Operation(
    name="memory.verify",
    input_model=MemoryVerifyInput,
    output_model=MemoryVerifyOutput,
    handler=handle_memory_verify,
    idempotent=True,
    required_role=Role.WRITER,
    # An attestation is a statement about truth made by a named actor —
    # the audit row is the record of who said so.
    audit=True,
    # memory_id addressing carries no namespace for the transport to check;
    # the handler re-checks access against the resolved row's namespace.
    skip_namespace_authz=True,
)


memory_statuses = Operation(
    name="memory.statuses",
    maturity="experimental",
    input_model=MemoryStatusesInput,
    output_model=MemoryStatusesOutput,
    handler=handle_memory_statuses,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)


memory_auto_retirements = Operation(
    name="memory.auto_retirements",
    maturity="experimental",
    input_model=MemoryAutoRetirementsInput,
    output_model=MemoryAutoRetirementsOutput,
    handler=handle_memory_auto_retirements,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)
