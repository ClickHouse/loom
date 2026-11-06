"""Operations over the wire models in `server.memory.api.models_lifecycle`."""

from __future__ import annotations

from server.memory.api.models_lifecycle import (
    MemoryCopyToInput,
    MemoryCopyToOutput,
    MemoryCrossPromoteInput,
    MemoryCrossPromoteOutput,
    MemoryDemoteInput,
    MemoryDemoteOutput,
    MemoryEvidenceInput,
    MemoryEvidenceOutput,
    MemoryPromoteInput,
    MemoryPromoteOutput,
    MemoryProvenanceInput,
    MemoryProvenanceOutput,
    MemoryRejectInput,
    MemoryRejectOutput,
    MemoryStackInput,
    MemoryStackOutput,
)
from server.memory.app.copy_ops import handle_memory_copy_to, handle_memory_cross_promote
from server.memory.app.evidence_reads import handle_memory_evidence, handle_memory_provenance
from server.memory.app.ops import handle_memory_reject
from server.memory.app.promote import handle_memory_demote, handle_memory_promote
from server.memory.app.served_reads import records_served, stack_keys
from server.memory.app.stack import handle_memory_stack
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_promote = Operation(
    name="memory.promote",
    maturity="experimental",
    input_model=MemoryPromoteInput,
    output_model=MemoryPromoteOutput,
    handler=handle_memory_promote,
    idempotent=False,  # state transition + audit
    required_role=Role.ADMIN,
    audit=True,
)

memory_demote = Operation(
    name="memory.demote",
    maturity="experimental",
    input_model=MemoryDemoteInput,
    output_model=MemoryDemoteOutput,
    handler=handle_memory_demote,
    idempotent=True,  # demoting a tombstoned row is a no-op
    required_role=Role.ADMIN,
    audit=True,
)

memory_reject = Operation(
    name="memory.reject",
    maturity="experimental",
    input_model=MemoryRejectInput,
    output_model=MemoryRejectOutput,
    handler=handle_memory_reject,
    idempotent=True,
    required_role=Role.WRITER,
    audit=True,
)

memory_cross_promote = Operation(
    name="memory.cross_promote",
    maturity="experimental",
    input_model=MemoryCrossPromoteInput,
    output_model=MemoryCrossPromoteOutput,
    handler=handle_memory_cross_promote,
    idempotent=False,
    required_role=Role.VIEWER,
    audit=True,
    # Operates ACROSS namespaces; the handler enforces ADMIN on both ends. The
    # transport check would fail on whichever namespace the JWT doesn't cover.
    skip_namespace_authz=True,
)

memory_copy_to = Operation(
    name="memory.copy_to",
    maturity="experimental",
    input_model=MemoryCopyToInput,
    output_model=MemoryCopyToOutput,
    handler=handle_memory_copy_to,
    idempotent=False,
    required_role=Role.WRITER,
    audit=True,
    # Spans namespaces; the handler enforces READ-on-source + WRITE-on-target
    # (no ADMIN). The transport's single-namespace check would fail on whichever
    # side the JWT doesn't directly cover, so it is skipped here.
    skip_namespace_authz=True,
)

memory_evidence = Operation(
    name="memory.evidence",
    input_model=MemoryEvidenceInput,
    output_model=MemoryEvidenceOutput,
    handler=handle_memory_evidence,
    idempotent=True,
    required_role=Role.VIEWER,
)

memory_provenance = Operation(
    name="memory.provenance",
    input_model=MemoryProvenanceInput,
    output_model=MemoryProvenanceOutput,
    handler=handle_memory_provenance,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

memory_stack = Operation(
    name="memory.stack",
    maturity="experimental",
    input_model=MemoryStackInput,
    output_model=MemoryStackOutput,
    # The keys its answer returns are recorded as `served` (server/memory/app/served_reads.py).
    handler=records_served(stack_keys)(handle_memory_stack),
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)
