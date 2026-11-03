"""The memory.* operations: one ``OPERATIONS`` list, the name the registry
imports, over the declarations in the ops_<group> modules (crud, search,
bulk, import, lifecycle, graph, temporal, admin, evidence_transfer, for_file,
move, proposals). Their wire models live in the matching models_<group> modules.

The SDK exposes `.share()` as an alias over `.promote()` — same wire
op; there's no separate `memory.share` op.
"""

from __future__ import annotations

from server.memory.api.ops_admin import (
    consolidations_list,
    memory_agents_list,
    memory_consolidate,
    memory_health,
    memory_reindex,
    memory_resolve_orphan_duplicates,
)
from server.memory.api.ops_ask import memory_ask
from server.memory.api.ops_bulk import (
    memory_bulk_get,
    memory_bulk_set,
    memory_extraction_retry_mine,
    memory_extraction_status,
    memory_extraction_status_mine,
    memory_import_curated,
    memory_set_from_messages,
)
from server.memory.api.ops_crud import (
    memory_apply,
    memory_auto_retirements,
    memory_clear,
    memory_delete,
    memory_delete_version,
    memory_get,
    memory_get_by_id,
    memory_keys,
    memory_list,
    memory_pin,
    memory_redact,
    memory_restore,
    memory_set,
    memory_statuses,
    memory_sweep_expired,
    memory_touch,
    memory_unpin,
    memory_update,
    memory_update_by_id,
    memory_verify,
)
from server.memory.api.ops_evidence_transfer import memory_export_evidence, memory_import_evidence
from server.memory.api.ops_exposure import memory_exposure_summary, memory_owner_report
from server.memory.api.ops_for_file import memory_for_file
from server.memory.api.ops_graph import (
    memory_assign_category,
    memory_dedup_collapse,
    memory_dedup_list,
    memory_dedup_resolve,
    memory_lineage,
    memory_link,
    memory_related,
)
from server.memory.api.ops_import import memory_import
from server.memory.api.ops_inspect import memory_inspect
from server.memory.api.ops_lifecycle import (
    memory_copy_to,
    memory_cross_promote,
    memory_demote,
    memory_evidence,
    memory_promote,
    memory_provenance,
    memory_reject,
    memory_stack,
)
from server.memory.api.ops_lock import memory_lock, memory_unlock
from server.memory.api.ops_move import memory_move
from server.memory.api.ops_proposals import memory_proposal_decide, memory_proposals
from server.memory.api.ops_search import (
    memory_aggregate,
    memory_search,
    memory_search_trace,
    memory_search_trace_log,
)
from server.memory.api.ops_temporal import (
    memory_backlinks,
    memory_digests,
    memory_history,
    memory_outcome,
    memory_outcome_run,
    memory_outline,
    memory_recall,
    memory_timeline,
)
from server.operations.base import Operation

# The registry (server.operations.__init__) imports this name; transports iterate it.
OPERATIONS: list[Operation] = [
    memory_get,
    memory_inspect,
    memory_set,
    memory_pin,
    memory_unpin,
    memory_delete,
    memory_delete_version,
    memory_restore,
    memory_statuses,
    memory_auto_retirements,
    memory_move,
    memory_clear,
    memory_keys,
    memory_list,
    memory_related,
    memory_for_file,
    memory_search,
    memory_search_trace,
    memory_search_trace_log,
    memory_exposure_summary,
    memory_owner_report,
    memory_aggregate,
    memory_ask,
    memory_promote,
    memory_demote,
    memory_bulk_set,
    memory_set_from_messages,
    memory_bulk_get,
    memory_extraction_status,
    memory_extraction_status_mine,
    memory_extraction_retry_mine,
    memory_touch,
    memory_lock,
    memory_unlock,
    memory_sweep_expired,
    memory_apply,
    memory_get_by_id,
    memory_update,
    memory_update_by_id,
    memory_verify,
    memory_redact,
    memory_digests,
    memory_outline,
    memory_backlinks,
    memory_history,
    memory_recall,
    memory_export_evidence,
    memory_import_evidence,
    memory_timeline,
    memory_lineage,
    memory_dedup_list,
    memory_dedup_resolve,
    memory_dedup_collapse,
    memory_outcome,
    memory_outcome_run,
    memory_cross_promote,
    memory_copy_to,
    memory_provenance,
    memory_evidence,
    memory_reject,
    memory_proposals,
    memory_proposal_decide,
    memory_consolidate,
    consolidations_list,
    memory_agents_list,
    memory_health,
    memory_reindex,
    memory_resolve_orphan_duplicates,
    memory_import_curated,
    memory_import,
    memory_link,
    memory_assign_category,
    memory_stack,
]
