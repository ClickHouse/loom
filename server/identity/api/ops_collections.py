"""The `collection` operations; wire models are in models_collections.py."""

from __future__ import annotations

from server.identity.api.models_collections import (
    CollectionCreateInput,
    CollectionCreateOutput,
    CollectionDeleteInput,
    CollectionDeleteOutput,
    CollectionListInput,
    CollectionListOutput,
    CollectionMoveInput,
    CollectionMoveOutput,
)
from server.identity.app.collections import (
    handle_collection_create,
    handle_collection_delete,
    handle_collection_list,
    handle_collection_move,
)
from server.operations.base import Operation, Role

collection_create = Operation(
    name="collection.create",
    maturity="experimental",
    input_model=CollectionCreateInput,
    output_model=CollectionCreateOutput,
    handler=handle_collection_create,
    idempotent=True,
    budget_p99_ms=80,
    # The collection does not exist yet; the handler checks writer on the
    # parent, and the creator becomes its admin.
    required_role=Role.VIEWER,
    audit=True,
    skip_namespace_authz=True,
)

collection_move = Operation(
    name="collection.move",
    maturity="experimental",
    input_model=CollectionMoveInput,
    output_model=CollectionMoveOutput,
    handler=handle_collection_move,
    idempotent=True,
    budget_p99_ms=80,
    # Admin on the collection moved; writer on the new parent, checked by the handler.
    required_role=Role.ADMIN,
    audit=True,
)

collection_list = Operation(
    name="collection.list",
    maturity="experimental",
    input_model=CollectionListInput,
    output_model=CollectionListOutput,
    handler=handle_collection_list,
    idempotent=True,
    budget_p99_ms=80,
    # Lists only what the caller's grants reach.
    required_role=Role.VIEWER,
    audit=False,
    skip_namespace_authz=True,
)

collection_delete = Operation(
    name="collection.delete",
    maturity="experimental",
    input_model=CollectionDeleteInput,
    output_model=CollectionDeleteOutput,
    handler=handle_collection_delete,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.ADMIN,
    audit=True,
)

OPERATIONS: list[Operation] = [
    collection_create,
    collection_move,
    collection_list,
    collection_delete,
]
