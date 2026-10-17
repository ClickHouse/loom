"""Operations over the wire models in `server.memory.api.models_temporal`."""

from __future__ import annotations

from server.core.feature_modes import bitemporal_on
from server.memory.api.models_temporal import (
    MemoryAmendInput,
    MemoryAmendOutput,
    MemoryBacklinksInput,
    MemoryBacklinksOutput,
    MemoryDigestsInput,
    MemoryDigestsOutput,
    MemoryHistoryInput,
    MemoryHistoryOutput,
    MemoryIntervalsInput,
    MemoryIntervalsOutput,
    MemoryOutcomeInput,
    MemoryOutcomeOutput,
    MemoryOutcomeRunInput,
    MemoryOutcomeRunOutput,
    MemoryOutlineInput,
    MemoryOutlineOutput,
    MemoryRecallInput,
    MemoryRecallOutput,
    MemoryTemporalDiffInput,
    MemoryTemporalDiffOutput,
    MemoryTemporalStatusInput,
    MemoryTemporalStatusOutput,
    MemoryTimelineInput,
    MemoryTimelineOutput,
)
from server.memory.app.curation import (
    handle_memory_backlinks,
    handle_memory_digests,
    handle_memory_history,
    handle_memory_outline,
    handle_memory_timeline,
)
from server.memory.app.ops import (
    handle_memory_outcome,
    handle_memory_outcome_run,
)
from server.memory.app.recall import handle_memory_recall
from server.memory.app.temporal_ops import (
    handle_memory_amend,
    handle_memory_intervals,
    handle_memory_temporal_diff,
    handle_memory_temporal_status,
)
from server.operations.base import Operation, Role

# ============================================================
# Operation declarations
# ============================================================

memory_digests = Operation(
    name="memory.digests",
    input_model=MemoryDigestsInput,
    output_model=MemoryDigestsOutput,
    handler=handle_memory_digests,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
)

memory_outline = Operation(
    name="memory.outline",
    maturity="experimental",
    input_model=MemoryOutlineInput,
    output_model=MemoryOutlineOutput,
    handler=handle_memory_outline,
    idempotent=True,
    budget_p99_ms=250,
    required_role=Role.VIEWER,
    audit=False,
)

memory_backlinks = Operation(
    name="memory.backlinks",
    maturity="experimental",
    input_model=MemoryBacklinksInput,
    output_model=MemoryBacklinksOutput,
    handler=handle_memory_backlinks,
    idempotent=True,
    budget_p99_ms=200,
    required_role=Role.VIEWER,
    audit=False,
)

memory_history = Operation(
    name="memory.history",
    maturity="experimental",
    input_model=MemoryHistoryInput,
    output_model=MemoryHistoryOutput,
    handler=handle_memory_history,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=False,
)


memory_recall = Operation(
    name="memory.recall",
    input_model=MemoryRecallInput,
    output_model=MemoryRecallOutput,
    handler=handle_memory_recall,
    idempotent=True,
    budget_p99_ms=400,
    required_role=Role.VIEWER,
)


memory_timeline = Operation(
    name="memory.timeline",
    maturity="experimental",
    input_model=MemoryTimelineInput,
    output_model=MemoryTimelineOutput,
    handler=handle_memory_timeline,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
)

memory_outcome = Operation(
    name="memory.outcome",
    maturity="experimental",
    input_model=MemoryOutcomeInput,
    output_model=MemoryOutcomeOutput,
    handler=handle_memory_outcome,
    idempotent=False,
    budget_p99_ms=200,
    required_role=Role.WRITER,
    audit=True,
)

memory_outcome_run = Operation(
    name="memory.outcome_run",
    input_model=MemoryOutcomeRunInput,
    output_model=MemoryOutcomeRunOutput,
    handler=handle_memory_outcome_run,
    idempotent=False,
    budget_p99_ms=500,
    required_role=Role.WRITER,
    audit=True,
)

memory_amend = Operation(
    name="memory.amend",
    maturity="experimental",
    enabled=bitemporal_on,
    input_model=MemoryAmendInput,
    output_model=MemoryAmendOutput,
    handler=handle_memory_amend,
    idempotent=True,
    budget_p99_ms=600,
    required_role=Role.WRITER,
    audit=True,
)

memory_intervals = Operation(
    name="memory.intervals",
    maturity="experimental",
    enabled=bitemporal_on,
    input_model=MemoryIntervalsInput,
    output_model=MemoryIntervalsOutput,
    handler=handle_memory_intervals,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)

memory_temporal_diff = Operation(
    name="memory.temporal_diff",
    maturity="experimental",
    enabled=bitemporal_on,
    input_model=MemoryTemporalDiffInput,
    output_model=MemoryTemporalDiffOutput,
    handler=handle_memory_temporal_diff,
    idempotent=True,
    budget_p99_ms=200,
    required_role=Role.VIEWER,
    audit=False,
)

memory_temporal_status = Operation(
    name="memory.temporal_status",
    maturity="experimental",
    enabled=bitemporal_on,
    input_model=MemoryTemporalStatusInput,
    output_model=MemoryTemporalStatusOutput,
    handler=handle_memory_temporal_status,
    idempotent=True,
    budget_p99_ms=150,
    required_role=Role.VIEWER,
    audit=False,
)
