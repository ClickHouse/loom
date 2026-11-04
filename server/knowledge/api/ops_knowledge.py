"""The `knowledge.*` operation declarations; their wire models are in `models_knowledge`."""

from __future__ import annotations

from server.knowledge.api.models_inspect import KnowledgeInspectInput, KnowledgeInspectOutput
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
    KnowledgeIngestCancelInput,
    KnowledgeIngestCancelOutput,
    KnowledgeIngestInput,
    KnowledgeIngestOutput,
    KnowledgeIngestStatusInput,
    KnowledgeIngestStatusOutput,
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
from server.knowledge.app.ingest import (
    handle_knowledge_ingest,
    handle_knowledge_ingest_cancel,
    handle_knowledge_ingest_status,
)
from server.knowledge.app.inspect import handle_knowledge_inspect
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
    required_role=Role.WRITER,
    audit=False,
)

knowledge_search = Operation(
    name="knowledge.search",
    input_model=KnowledgeSearchInput,
    output_model=KnowledgeSearchOutput,
    handler=handle_knowledge_search,
    idempotent=True,
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
    required_role=Role.WRITER,
    audit=True,
)

knowledge_list = Operation(
    name="knowledge.list",
    input_model=KnowledgeListInput,
    output_model=KnowledgeListOutput,
    handler=handle_knowledge_list,
    idempotent=True,
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
    required_role=Role.WRITER,
    audit=True,
)

knowledge_bulk_add = Operation(
    name="knowledge.bulk_add",
    input_model=KnowledgeBulkAddInput,
    output_model=KnowledgeBulkAddOutput,
    handler=handle_knowledge_bulk_add,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
)

knowledge_get = Operation(
    name="knowledge.get",
    input_model=KnowledgeGetInput,
    output_model=KnowledgeGetOutput,
    handler=handle_knowledge_get,
    idempotent=True,
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
    required_role=Role.ADMIN,
    audit=True,
)


# Extraction of an uploaded file (POST /v1/knowledge.upload) by the extractor
# service, and its publication, as a job.
knowledge_ingest = Operation(
    name="knowledge.ingest",
    input_model=KnowledgeIngestInput,
    output_model=KnowledgeIngestOutput,
    handler=handle_knowledge_ingest,
    idempotent=False,  # each call queues a job, which publishes a revision
    required_role=Role.WRITER,
    audit=False,
    maturity="experimental",
)

knowledge_ingest_status = Operation(
    name="knowledge.ingest_status",
    input_model=KnowledgeIngestStatusInput,
    output_model=KnowledgeIngestStatusOutput,
    handler=handle_knowledge_ingest_status,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
    maturity="experimental",
)

knowledge_ingest_cancel = Operation(
    name="knowledge.ingest_cancel",
    input_model=KnowledgeIngestCancelInput,
    output_model=KnowledgeIngestCancelOutput,
    handler=handle_knowledge_ingest_cancel,
    idempotent=True,
    required_role=Role.WRITER,
    audit=False,
    maturity="experimental",
)

knowledge_inspect = Operation(
    name="knowledge.inspect",
    maturity="experimental",
    input_model=KnowledgeInspectInput,
    output_model=KnowledgeInspectOutput,
    handler=handle_knowledge_inspect,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
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
    knowledge_inspect,
    knowledge_synthesize,
    knowledge_bulk_add,
    knowledge_bulk_delete,
    knowledge_dedup_list,
    knowledge_dedup_resolve,
    knowledge_ingest,
    knowledge_ingest_status,
    knowledge_ingest_cancel,
]
