"""Operations over the wire models in `server.memory.api.models_blacklist`."""

from __future__ import annotations

from server.memory.api.models_blacklist import (
    BlacklistAddInput,
    BlacklistAddOutput,
    BlacklistListInput,
    BlacklistListOutput,
    BlacklistRemoveInput,
    BlacklistRemoveOutput,
    BlacklistSweepInput,
    BlacklistSweepOutput,
)
from server.memory.app.blacklist import (
    handle_blacklist_add,
    handle_blacklist_list,
    handle_blacklist_remove,
    handle_blacklist_sweep,
)
from server.operations.base import Operation, Role

# WRITER, not ADMIN: blacklist.add is the in-band Refuse verb ("never
# remember X") and ships in the MCP core toolset — a consent request
# mid-conversation must be honorable by a writer-grade agent session
# without a dashboard round-trip. The default scope is `namespace`,
# which a writer already controls. A wider scope is gated in the handler
# by the role whose reach matches it: scope="org" needs
# ORG_ADMIN, scope="global" PLATFORM_ADMIN. A regex pattern is at most 256
# characters and runs on RE2, in time linear in the value, because it runs
# on every write its scope covers.
blacklist_add = Operation(
    name="blacklist.add",
    maturity="experimental",
    input_model=BlacklistAddInput,
    output_model=BlacklistAddOutput,
    handler=handle_blacklist_add,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

blacklist_list = Operation(
    name="blacklist.list",
    input_model=BlacklistListInput,
    output_model=BlacklistListOutput,
    handler=handle_blacklist_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

blacklist_remove = Operation(
    name="blacklist.remove",
    maturity="experimental",
    input_model=BlacklistRemoveInput,
    output_model=BlacklistRemoveOutput,
    handler=handle_blacklist_remove,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
    row_scoped=True,  # The handler authorizes the row's namespace
)


# The sweep reaches rows written before the pattern; it needs what add
# needs, on the PATTERN's reach, not the body's namespace: the handler asks
# WRITER on a namespace pattern's own namespace (a foreign one answers 404,
# like an unknown id) and ORG_ADMIN of this org for an org or global pattern.
# It is idempotent: a swept row is tombstoned and no longer matches
# `tombstoned_at IS NULL`.
blacklist_sweep = Operation(
    name="blacklist.sweep",
    maturity="experimental",
    input_model=BlacklistSweepInput,
    output_model=BlacklistSweepOutput,
    handler=handle_blacklist_sweep,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)
OPERATIONS: list[Operation] = [blacklist_add, blacklist_list, blacklist_remove, blacklist_sweep]
