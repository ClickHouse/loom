"""The `conversation.*` operation declarations; their wire models are in `models_conversations`."""

from __future__ import annotations

from server.knowledge.api.models_conversations import (
    ConversationAppendInput,
    ConversationAppendOutput,
    ConversationCompactInput,
    ConversationCompactOutput,
    ConversationCreateInput,
    ConversationCreateOutput,
    ConversationDeleteInput,
    ConversationDeleteOutput,
    ConversationGetInput,
    ConversationGetOutput,
    ConversationListInput,
    ConversationListOutput,
    ConversationSearchInput,
    ConversationSearchOutput,
    ConversationSetLoggingInput,
    ConversationSetLoggingOutput,
    ConversationThreadsInput,
    ConversationThreadsOutput,
    ConversationUpdateInput,
    ConversationUpdateOutput,
)
from server.knowledge.app.conversations import (
    handle_conversation_append,
    handle_conversation_compact,
    handle_conversation_create,
    handle_conversation_delete,
    handle_conversation_get,
    handle_conversation_list,
    handle_conversation_search,
    handle_conversation_set_logging,
    handle_conversation_threads,
    handle_conversation_update,
)
from server.operations.base import Operation, Role

conversation_create = Operation(
    name="conversation.create",
    maturity="experimental",
    input_model=ConversationCreateInput,
    output_model=ConversationCreateOutput,
    handler=handle_conversation_create,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)

conversation_append = Operation(
    name="conversation.append",
    input_model=ConversationAppendInput,
    output_model=ConversationAppendOutput,
    handler=handle_conversation_append,
    idempotent=True,  # dedup on msg_id
    required_role=Role.WRITER,
    audit=False,
)

conversation_set_logging = Operation(
    name="conversation.set_logging",
    maturity="experimental",
    input_model=ConversationSetLoggingInput,
    output_model=ConversationSetLoggingOutput,
    handler=handle_conversation_set_logging,
    idempotent=True,
    # ALTER UPDATE mutation
    required_role=Role.ADMIN,  # changing a namespace setting
    audit=True,
)

conversation_list = Operation(
    name="conversation.list",
    maturity="experimental",
    input_model=ConversationListInput,
    output_model=ConversationListOutput,
    handler=handle_conversation_list,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

conversation_get = Operation(
    name="conversation.get",
    maturity="experimental",
    input_model=ConversationGetInput,
    output_model=ConversationGetOutput,
    handler=handle_conversation_get,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

conversation_update = Operation(
    name="conversation.update",
    maturity="experimental",
    input_model=ConversationUpdateInput,
    output_model=ConversationUpdateOutput,
    handler=handle_conversation_update,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)

conversation_delete = Operation(
    name="conversation.delete",
    maturity="experimental",
    input_model=ConversationDeleteInput,
    output_model=ConversationDeleteOutput,
    handler=handle_conversation_delete,
    idempotent=True,
    # three bulk ALTER UPDATEs, mutations_sync=2
    required_role=Role.ADMIN,
    audit=True,
)

conversation_search = Operation(
    name="conversation.search",
    maturity="experimental",
    input_model=ConversationSearchInput,
    output_model=ConversationSearchOutput,
    handler=handle_conversation_search,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

conversation_threads = Operation(
    name="conversation.threads",
    input_model=ConversationThreadsInput,
    output_model=ConversationThreadsOutput,
    handler=handle_conversation_threads,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)


conversation_compact = Operation(
    name="conversation.compact",
    maturity="experimental",
    input_model=ConversationCompactInput,
    output_model=ConversationCompactOutput,
    handler=handle_conversation_compact,
    idempotent=True,
    # dominated by the LLM round-trip
    required_role=Role.WRITER,
    audit=True,
)


OPERATIONS: list[Operation] = [
    conversation_create,
    conversation_append,
    conversation_set_logging,
    conversation_get,
    conversation_update,
    conversation_delete,
    conversation_list,
    conversation_search,
    conversation_threads,
    conversation_compact,
]
