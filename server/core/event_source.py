"""The complete event-row source shared by scoped compilers and trusted stores.

The explicit columns keep an added private field out of shared projections until its
readers classify it. This module builds SQL fragments and performs no I/O.
"""

from __future__ import annotations

EVENT_COLUMNS = (
    "ts",
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "run_id",
    "operation",
    "request_id",
    "trace_id",
    "memory_key",
    "impersonated_by",
    "kind",
    "build_commit",
    "instance",
    "instance_label",
    "consumer",
    "traffic_class",
    "subject",
    "transport",
    "slo_class",
    "budget_ms",
    "ingested_at",
    "mirror_marked",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "embedding_tokens",
    "upstream_cost_usd",
    "bytes_written",
    "success",
    "error_code",
    "args",
    "model",
    "llm_calls",
    "response_tokens",
    "status_class",
)


def event_source(db: str = "loom") -> str:
    """Both physical streams; `db` is a quoted internal database, never caller SQL."""
    selected = ", ".join(EVENT_COLUMNS)
    return (
        f"(SELECT {selected} FROM {db}.agent_events UNION ALL "
        f"SELECT {selected} FROM {db}.agent_events_private)"
    )


ALL_EVENTS = event_source()
