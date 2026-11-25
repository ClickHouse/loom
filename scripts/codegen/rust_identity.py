"""Field roles for Rust calls whose memory identity can also select stored rows."""

from __future__ import annotations

from typing import Any

TUPLE_FIELDS = {"agent", "user_id", "session_id", "run_id"}

# These operations create content under the client's writer identity.
AUTHORED_MEMORY = {
    "memory.set",
    "memory.pin",
    "memory.bulk_set",
    "memory.append_batch",
    "memory.set_from_messages",
    "memory.import",
    "context.offload",
    "profile.set",
    "working_memory.set",
    "session.brief_save",
}

# The tuple selects a row, a query, or the inputs to a composed read. Explicit
# values survive; omitted values retain the client's personal-view defaults.
TARGETED_MEMORY = {
    "memory.get",
    "memory.unpin",
    "memory.delete",
    "memory.delete_version",
    "memory.restore",
    "memory.move",
    "memory.list",
    "memory.related",
    "memory.aggregate",
    "memory.bulk_get",
    "memory.touch",
    "memory.lock",
    "memory.unlock",
    "memory.apply",
    "memory.digests",
    "memory.outline",
    "memory.backlinks",
    "memory.history",
    "memory.timeline",
    "memory.lineage",
    "memory.provenance",
    "memory.evidence",
    "memory.reject",
    "memory.consolidate",
    "consolidations.list",
    "memory.agents_list",
    "memory.health",
    "memory.stack",
    "context.pack",
    "context.fetch",
    "profile.render",
    "profile.append",
    "profile.replace",
    "profile.activity",
    "profile.structured",
    "working_memory.get",
    "working_memory.append",
    "working_memory.replace",
    "working_memory.delete",
    "working_memory.render",
    "session.brief_get",
    "session.brief_list",
    "session.brief_delete",
}

# Optional filters keep server defaults. Required agent fields still get a
# client default so the typed constructor can omit a caller-supplied agent.
FILTERED_MEMORY = {
    "memory.ask",
    "memory.extraction_status",
    "memory.recall",
    "memory.brief",
}

NAMESPACE_FILTERS = {
    "memory.owner_report",
    "learnings.quality",
    "cost.summary",
    "cost.budget_status",
    "cost.agent_summary",
    "audit.list",
    "ops.health",
    "ops.projection_failures.list",
    "ops.projection_failures.retry",
    "agents.connected",
    "admin.users",
}

PROCEDURE_TARGETS = {"procedure.evolve", "procedure.revise", "procedure.record_run"}

SOURCE_TARGETS = {"memory.promote", "memory.verify"}
CONVERSATION_TARGETS = {
    "conversation.update",
    "conversation.delete",
    "conversation.compact",
    "conversation.unsave_message",
}


def memory_tuple_policy(op: Any) -> tuple[set[str], set[str]]:
    fields = op.input_model.model_fields
    keys = fields.keys() & TUPLE_FIELDS
    if op.name in AUTHORED_MEMORY:
        return set(keys), set()
    if op.name in TARGETED_MEMORY:
        defaults = set(keys)
        if op.name == "memory.list":
            defaults.discard("run_id")
        if op.name == "memory.digests":
            defaults.discard("session_id")
        return set(), defaults
    if op.name in FILTERED_MEMORY:
        return set(), {key for key in keys if fields[key].is_required()}
    raise ValueError(f"unclassified Rust memory identity: {op.name}")


def validate_roles(operations: list[Any]) -> None:
    from server.memory.api.models_common import _Identity

    memory_ops = {op.name for op in operations if issubclass(op.input_model, _Identity)}
    classified = AUTHORED_MEMORY | TARGETED_MEMORY | FILTERED_MEMORY
    if memory_ops != classified:
        raise ValueError(f"Rust memory identity census changed: {sorted(memory_ops ^ classified)}")
    nullable_namespaces = {
        op.name
        for op in operations
        if "namespace" in op.input_model.model_fields
        and op.input_model.model_fields["namespace"].default is None
    }
    if nullable_namespaces != NAMESPACE_FILTERS:
        raise ValueError(
            f"Rust namespace filter census changed: "
            f"{sorted(nullable_namespaces ^ NAMESPACE_FILTERS)}"
        )
