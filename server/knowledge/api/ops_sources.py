"""The `knowledge.source.*` operation declarations; their wire models are in
`models_sources`. Each op is addressed by the repository's trunk code namespace,
so the transport's namespace gate decides: readers list and inspect, the
namespace's admins (the repository's maintainers) do the rest."""

from __future__ import annotations

from server.knowledge.api.models_sources import (
    KnowledgeSourceAddInput,
    KnowledgeSourceAddOutput,
    KnowledgeSourceCheckInput,
    KnowledgeSourceCheckOutput,
    KnowledgeSourceInspectInput,
    KnowledgeSourceInspectOutput,
    KnowledgeSourceListInput,
    KnowledgeSourceListOutput,
    KnowledgeSourceRemoveInput,
    KnowledgeSourceRemoveOutput,
    KnowledgeSourceRotateInput,
    KnowledgeSourceRotateOutput,
    KnowledgeSourceSyncNowInput,
    KnowledgeSourceSyncNowOutput,
)
from server.knowledge.app.sources import (
    handle_knowledge_source_add,
    handle_knowledge_source_check,
    handle_knowledge_source_inspect,
    handle_knowledge_source_list,
    handle_knowledge_source_remove,
    handle_knowledge_source_rotate,
    handle_knowledge_source_sync_now,
    source_add_audit_subject,
    source_rotate_audit_subject,
)
from server.operations.base import Operation, Role

knowledge_source_list = Operation(
    name="knowledge.source.list",
    maturity="experimental",
    input_model=KnowledgeSourceListInput,
    output_model=KnowledgeSourceListOutput,
    handler=handle_knowledge_source_list,
    idempotent=True,
    required_role=Role.VIEWER,
)
knowledge_source_inspect = Operation(
    name="knowledge.source.inspect",
    maturity="experimental",
    input_model=KnowledgeSourceInspectInput,
    output_model=KnowledgeSourceInspectOutput,
    handler=handle_knowledge_source_inspect,
    idempotent=True,
    required_role=Role.VIEWER,
)
knowledge_source_add = Operation(
    name="knowledge.source.add",
    maturity="experimental",
    input_model=KnowledgeSourceAddInput,
    output_model=KnowledgeSourceAddOutput,
    handler=handle_knowledge_source_add,
    idempotent=False,
    required_role=Role.ADMIN,
    audit=True,
    audit_subject=source_add_audit_subject,
)
# ADMIN although it writes nothing: the probe spends the source's rate limit
# with the stored credential.
knowledge_source_check = Operation(
    name="knowledge.source.check",
    maturity="experimental",
    input_model=KnowledgeSourceCheckInput,
    output_model=KnowledgeSourceCheckOutput,
    handler=handle_knowledge_source_check,
    idempotent=True,
    required_role=Role.ADMIN,
)
knowledge_source_rotate = Operation(
    name="knowledge.source.rotate",
    maturity="experimental",
    input_model=KnowledgeSourceRotateInput,
    output_model=KnowledgeSourceRotateOutput,
    handler=handle_knowledge_source_rotate,
    idempotent=False,
    required_role=Role.ADMIN,
    audit=True,
    audit_subject=source_rotate_audit_subject,
)
knowledge_source_remove = Operation(
    name="knowledge.source.remove",
    maturity="experimental",
    input_model=KnowledgeSourceRemoveInput,
    output_model=KnowledgeSourceRemoveOutput,
    handler=handle_knowledge_source_remove,
    idempotent=False,
    required_role=Role.ADMIN,
    audit=True,
)
knowledge_source_sync_now = Operation(
    name="knowledge.source.sync_now",
    maturity="experimental",
    input_model=KnowledgeSourceSyncNowInput,
    output_model=KnowledgeSourceSyncNowOutput,
    handler=handle_knowledge_source_sync_now,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)

OPERATIONS: list[Operation] = [
    knowledge_source_list,
    knowledge_source_inspect,
    knowledge_source_add,
    knowledge_source_check,
    knowledge_source_rotate,
    knowledge_source_remove,
    knowledge_source_sync_now,
]
