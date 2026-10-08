"""The `knowledge.*` operation declarations; their wire models are in `models_knowledge`."""

from __future__ import annotations

from server.knowledge.api.models_knowledge import (
    KnowledgeAddInput,
    KnowledgeAddOutput,
    KnowledgeBulkAddInput,
    KnowledgeBulkAddOutput,
    KnowledgeBulkDeleteInput,
    KnowledgeBulkDeleteOutput,
    KnowledgeDedupListInput,
    KnowledgeDedupListOutput,
    KnowledgeDedupResolveInput,
    KnowledgeDedupResolveOutput,
    KnowledgeDeleteInput,
    KnowledgeDeleteOutput,
    KnowledgeGetInput,
    KnowledgeGetOutput,
    KnowledgeListInput,
    KnowledgeListOutput,
    KnowledgeOutlineInput,
    KnowledgeOutlineOutput,
    KnowledgeReadsInput,
    KnowledgeReadsOutput,
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    KnowledgeSynthesizeInput,
    KnowledgeSynthesizeOutput,
    KnowledgeUpdateMetadataInput,
    KnowledgeUpdateMetadataOutput,
    KnowledgeUpsertInput,
    KnowledgeUpsertOutput,
)
from server.knowledge.app.dedup import handle_knowledge_dedup_list, handle_knowledge_dedup_resolve
from server.knowledge.app.document_reads import (
    handle_knowledge_get,
    handle_knowledge_list,
    handle_knowledge_outline,
)
from server.knowledge.app.knowledge_search import handle_knowledge_search
from server.knowledge.app.reads import handle_knowledge_reads
from server.knowledge.app.synthesize import handle_knowledge_synthesize
from server.knowledge.app.writes import (
    handle_knowledge_add,
    handle_knowledge_bulk_add,
    handle_knowledge_bulk_delete,
    handle_knowledge_delete,
    handle_knowledge_update_metadata,
    handle_knowledge_upsert,
)
from server.operations.base import Operation, Role

knowledge_add = Operation(
    name="knowledge.add",
    input_model=KnowledgeAddInput,
    output_model=KnowledgeAddOutput,
    handler=handle_knowledge_add,
    idempotent=True,  # on (org, namespace, source_uri); see handler
    budget_p99_ms=50,
    required_role=Role.WRITER,
    audit=False,
)

knowledge_upsert = Operation(
    name="knowledge.upsert",
    maturity="experimental",
    input_model=KnowledgeUpsertInput,
    output_model=KnowledgeUpsertOutput,
    handler=handle_knowledge_upsert,
    idempotent=True,
    budget_p99_ms=100,
    required_role=Role.WRITER,
    audit=False,
)

knowledge_search = Operation(
    name="knowledge.search",
    input_model=KnowledgeSearchInput,
    output_model=KnowledgeSearchOutput,
    handler=handle_knowledge_search,
    idempotent=True,
    budget_p99_ms=250,
    required_role=Role.VIEWER,
    audit=False,
)

knowledge_delete = Operation(
    name="knowledge.delete",
    maturity="experimental",
    input_model=KnowledgeDeleteInput,
    output_model=KnowledgeDeleteOutput,
    handler=handle_knowledge_delete,
    idempotent=True,  # re-deleting an already-deleted doc is a no-op
    budget_p99_ms=100,
    required_role=Role.WRITER,
    audit=True,
)

knowledge_update_metadata = Operation(
    name="knowledge.update_metadata",
    maturity="experimental",
    input_model=KnowledgeUpdateMetadataInput,
    output_model=KnowledgeUpdateMetadataOutput,
    handler=handle_knowledge_update_metadata,
    idempotent=True,  # re-applying the same edit is a no-op
    budget_p99_ms=60,
    required_role=Role.WRITER,
    audit=True,
)

knowledge_list = Operation(
    name="knowledge.list",
    input_model=KnowledgeListInput,
    output_model=KnowledgeListOutput,
    handler=handle_knowledge_list,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)

knowledge_bulk_delete = Operation(
    name="knowledge.bulk_delete",
    maturity="experimental",
    input_model=KnowledgeBulkDeleteInput,
    output_model=KnowledgeBulkDeleteOutput,
    handler=handle_knowledge_bulk_delete,
    idempotent=True,
    budget_p99_ms=500,
    required_role=Role.WRITER,
    audit=True,
)

knowledge_bulk_add = Operation(
    name="knowledge.bulk_add",
    input_model=KnowledgeBulkAddInput,
    output_model=KnowledgeBulkAddOutput,
    handler=handle_knowledge_bulk_add,
    idempotent=True,
    budget_p99_ms=2000,
    required_role=Role.WRITER,
    audit=False,
)

knowledge_get = Operation(
    name="knowledge.get",
    input_model=KnowledgeGetInput,
    output_model=KnowledgeGetOutput,
    handler=handle_knowledge_get,
    idempotent=True,
    budget_p99_ms=30,
    required_role=Role.VIEWER,
    audit=False,
)


# One primary-key range read of agent_events per call.
knowledge_reads = Operation(
    name="knowledge.reads",
    input_model=KnowledgeReadsInput,
    output_model=KnowledgeReadsOutput,
    handler=handle_knowledge_reads,
    idempotent=True,
    budget_p99_ms=500,
    required_role=Role.VIEWER,
    audit=False,
    maturity="experimental",
)


knowledge_dedup_list = Operation(
    name="knowledge.dedup_list",
    maturity="experimental",
    input_model=KnowledgeDedupListInput,
    output_model=KnowledgeDedupListOutput,
    handler=handle_knowledge_dedup_list,
    idempotent=True,
    budget_p99_ms=400,
    required_role=Role.ADMIN,
    audit=False,
)

knowledge_synthesize = Operation(
    name="knowledge.synthesize",
    maturity="experimental",
    input_model=KnowledgeSynthesizeInput,
    output_model=KnowledgeSynthesizeOutput,
    handler=handle_knowledge_synthesize,
    idempotent=True,
    # One LLM call per window plus the reduce; bounded by max_windows.
    budget_p99_ms=60000,
    required_role=Role.VIEWER,
    audit=True,
)

knowledge_outline = Operation(
    name="knowledge.outline",
    maturity="experimental",
    input_model=KnowledgeOutlineInput,
    output_model=KnowledgeOutlineOutput,
    handler=handle_knowledge_outline,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=False,
)

knowledge_dedup_resolve = Operation(
    name="knowledge.dedup_resolve",
    maturity="experimental",
    input_model=KnowledgeDedupResolveInput,
    output_model=KnowledgeDedupResolveOutput,
    handler=handle_knowledge_dedup_resolve,
    idempotent=True,
    budget_p99_ms=400,
    required_role=Role.ADMIN,
    audit=True,
)


OPERATIONS: list[Operation] = [
    knowledge_add,
    knowledge_reads,
    knowledge_upsert,
    knowledge_search,
    knowledge_delete,
    knowledge_update_metadata,
    knowledge_list,
    knowledge_get,
    knowledge_outline,
    knowledge_synthesize,
    knowledge_bulk_add,
    knowledge_bulk_delete,
    knowledge_dedup_list,
    knowledge_dedup_resolve,
]
