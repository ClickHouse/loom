"""The operation registry: every wire-op group, flattened and indexed by name.

Transports (HTTP, MCP), the SDK generators and the docs read ALL_OPERATIONS
and OPERATIONS_BY_NAME from here. Adding an operation = declare it in its
group module's ``OPERATIONS`` list; adding a group = import the module and
append it to ``_OPERATION_MODULES``. The package ``__init__`` imports nothing,
so a domain api module can import ``server.operations.base`` without loading
every other domain.
"""

from __future__ import annotations

from server.code.api import ops as code
from server.code.api import ops_coverage_watch as code_watch
from server.erasure.api import ops as erasure
from server.identity.api import ops_admin_users as admin_users
from server.identity.api import ops_collections as collections
from server.identity.api import ops_grants as grants
from server.identity.api import ops_model_calls as model_calls
from server.identity.api import ops_namespace as namespace
from server.identity.api import ops_policy as policy
from server.insights.api import ops_agents as agents
from server.insights.api import ops_audit as audit
from server.insights.api import ops_cost as cost
from server.insights.api import ops_mentor as mentor
from server.insights.api import ops_ops as ops
from server.insights.api import ops_ui as ui
from server.knowledge.api import ops_conversations as conversations
from server.knowledge.api import ops_knowledge as knowledge
from server.knowledge.api import ops_sources as knowledge_sources
from server.memory.api import ops_blacklist as blacklist
from server.memory.api import ops_brief as memory_brief
from server.memory.api import ops_calibration as calibration
from server.memory.api import ops_conflicts as conflicts
from server.memory.api import ops_context as context
from server.memory.api import ops_context_offload as context_offload
from server.memory.api import ops_decision as decision
from server.memory.api import ops_demonstrate as demonstrate
from server.memory.api import ops_extraction_policy as extraction_policy
from server.memory.api import ops_facts as facts
from server.memory.api import ops_learnings as learnings
from server.memory.api import ops_loom_search as search
from server.memory.api import ops_memory as memory
from server.memory.api import ops_observe as observe
from server.memory.api import ops_procedure as procedure
from server.memory.api import ops_profile as profile
from server.memory.api import ops_records as memory_records
from server.memory.api import ops_session_brief as session_brief
from server.memory.api import ops_webhook as webhook
from server.memory.api import ops_working_memory as working_memory
from server.operations.base import Operation
from server.tasks.api import ops_run as run
from server.tasks.api import ops_task as task
from server.tracker.api import ops as tracker

# One entry per wire-op group. Order is not semantic.
_OPERATION_MODULES = (
    memory,
    memory_brief,
    memory_records,
    knowledge,
    knowledge_sources,
    conversations,
    context,
    context_offload,
    learnings,
    blacklist,
    webhook,
    extraction_policy,
    conflicts,
    erasure,
    cost,
    decision,
    demonstrate,
    audit,
    namespace,
    mentor,
    ops,
    grants,
    collections,
    agents,
    admin_users,
    model_calls,
    facts,
    profile,
    working_memory,
    session_brief,
    procedure,
    search,
    observe,
    run,
    calibration,
    code,
    code_watch,
    policy,
    tracker,
    task,
    ui,
)


def index_by_name(ops: list[Operation]) -> dict[str, Operation]:
    """The registry keyed by op name. Raises ValueError when two ops share a
    name or an MCP tool name (`a.b_c` and `a_b.c` both derive `loom_a_b_c`):
    a dict built over duplicates keeps the last one silently, so one op
    would shadow the other on HTTP, MCP and the SDK tables alike."""
    by_name: dict[str, Operation] = {}
    by_tool: dict[str, str] = {}
    for op in ops:
        if op.name in by_name:
            raise ValueError(f"operation {op.name!r} is declared twice")
        if op.mcp_tool_name in by_tool:
            raise ValueError(
                f"operations {by_tool[op.mcp_tool_name]!r} and {op.name!r} "
                f"share the MCP tool name {op.mcp_tool_name!r}"
            )
        by_name[op.name] = op
        by_tool[op.mcp_tool_name] = op.name
    return by_name


ALL_OPERATIONS: list[Operation] = [op for mod in _OPERATION_MODULES for op in mod.OPERATIONS]

OPERATIONS_BY_NAME: dict[str, Operation] = index_by_name(ALL_OPERATIONS)


__all__ = ["ALL_OPERATIONS", "OPERATIONS_BY_NAME", "index_by_name"]
