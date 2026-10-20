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
    ConversationListSavedMessagesInput,
    ConversationListSavedMessagesOutput,
    ConversationSavedStatusInput,
    ConversationSavedStatusOutput,
    ConversationSaveMessageInput,
    ConversationSaveMessageOutput,
    ConversationSearchInput,
    ConversationSearchOutput,
    ConversationSearchSavedMessagesInput,
    ConversationSearchSavedMessagesOutput,
    ConversationSetLoggingInput,
    ConversationSetLoggingOutput,
    ConversationThreadsInput,
    ConversationThreadsOutput,
    ConversationUnsaveMessageInput,
    ConversationUnsaveMessageOutput,
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
    handle_conversation_list_saved_messages,
    handle_conversation_save_message,
    handle_conversation_saved_status,
    handle_conversation_search,
    handle_conversation_search_saved_messages,
    handle_conversation_set_logging,
    handle_conversation_threads,
    handle_conversation_unsave_message,
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

conversation_save_message = Operation(
    name="conversation.save_message",
    maturity="experimental",
    input_model=ConversationSaveMessageInput,
    output_model=ConversationSaveMessageOutput,
    handler=handle_conversation_save_message,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)

conversation_unsave_message = Operation(
    name="conversation.unsave_message",
    maturity="experimental",
    input_model=ConversationUnsaveMessageInput,
    output_model=ConversationUnsaveMessageOutput,
    handler=handle_conversation_unsave_message,
    idempotent=True,
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

conversation_search_saved_messages = Operation(
    name="conversation.search_saved_messages",
    maturity="experimental",
    input_model=ConversationSearchSavedMessagesInput,
    output_model=ConversationSearchSavedMessagesOutput,
    handler=handle_conversation_search_saved_messages,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

conversation_list_saved_messages = Operation(
    name="conversation.list_saved_messages",
    maturity="experimental",
    input_model=ConversationListSavedMessagesInput,
    output_model=ConversationListSavedMessagesOutput,
    handler=handle_conversation_list_saved_messages,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

conversation_saved_status = Operation(
    name="conversation.saved_status",
    maturity="experimental",
    input_model=ConversationSavedStatusInput,
    output_model=ConversationSavedStatusOutput,
    handler=handle_conversation_saved_status,
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
    required_role=Role.WRITER,
    audit=True,
)


OPERATIONS: list[Operation] = [
    conversation_create,
    conversation_append,
    conversation_save_message,
    conversation_unsave_message,
    conversation_set_logging,
    conversation_get,
    conversation_update,
    conversation_delete,
    conversation_list,
    conversation_search,
    conversation_search_saved_messages,
    conversation_list_saved_messages,
    conversation_saved_status,
    conversation_threads,
    conversation_compact,
]
