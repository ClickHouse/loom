"""Marked event columns for readers that retain their own authorization scope."""

from __future__ import annotations

from server.core.event_source import ALL_EVENTS
from server.core.naming import not_mirror_sql

# This explicit projection never publishes an added column by default.
_PARTICIPATION = (
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
)

_PROTECTED = (
    ("latency_ms", "UInt32", "0"),
    ("input_tokens", "UInt32", "0"),
    ("output_tokens", "UInt32", "0"),
    ("embedding_tokens", "UInt32", "0"),
    ("upstream_cost_usd", "Decimal(18, 6)", "0"),
    ("bytes_written", "UInt64", "0"),
    ("success", "Bool", "false"),
    ("error_code", "LowCardinality(String)", "''"),
    ("args", "Map(LowCardinality(String), String)", "map()"),
    ("model", "LowCardinality(String)", "''"),
    ("llm_calls", "UInt16", "0"),
    ("response_tokens", "UInt32", "0"),
    ("status_class", "LowCardinality(String)", "''"),
)

MASKED_EVENT_COLUMNS = ", ".join(
    (
        *(f"raw.{name} AS {name}" for name in _PARTICIPATION),
        *(
            f"CAST(if(raw.mirror_marked != 0, CAST({neutral}, '{kind}'), raw.{name}), "
            f"'{kind}') AS {name}"
            for name, kind, neutral in _PROTECTED
        ),
    )
)

# Scope and result predicates belong outside this projection; its fields are masked first.
MASKED_EVENTS = (
    f"(SELECT {MASKED_EVENT_COLUMNS} FROM {ALL_EVENTS} AS raw "
    f"WHERE {not_mirror_sql('raw.namespace')})"
)
