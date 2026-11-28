"""Operations on selected resources at an approved document source worker."""

from server.core.operation_privacy import KNOWLEDGE_SOURCE_LIVE
from server.knowledge.api.models_source_live import (
    KnowledgeSourceLiveInput,
    KnowledgeSourceLiveOutput,
)
from server.knowledge.app.mcp_worker_live_op import handle_knowledge_source_live
from server.operations.base import Operation, Role

knowledge_source_live = Operation(
    name=KNOWLEDGE_SOURCE_LIVE,
    input_model=KnowledgeSourceLiveInput,
    output_model=KnowledgeSourceLiveOutput,
    handler=handle_knowledge_source_live,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
    maturity="experimental",
)

OPERATIONS = [knowledge_source_live]
