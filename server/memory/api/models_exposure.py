"""memory.exposure_summary and memory.owner_report: who read how much of a
namespace, and what an owner's rows are and how they were used."""

from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    Field,
)

# ============================================================
# memory.exposure_summary — who saw how much of this namespace
# ============================================================


class ExposureSummaryInput(BaseModel):
    """Per-(actor, agent) distinct memory keys returned by reads of a
    namespace over a window — how many distinct records each token has
    extracted — computed for every token, from
    loom.memory_exposure_breadth_hourly."""

    org: str
    namespace: str
    # Look-back window in hours, 1 to 8760 (one year).
    since_hours: int = Field(default=24, ge=1, le=8760)
    # At most this many (actor, agent) rows, most distinct keys first; capped at 1000.
    limit: int = Field(default=50, ge=1, le=1000)


class _ExposureSummaryRow(BaseModel):
    actor: str
    agent: str
    distinct_keys: int
    calls: int
    # distinct_keys / live rows in the namespace; 0 when the namespace is empty.
    fraction: float
    first_hour: datetime
    last_hour: datetime


class ExposureSummaryOutput(BaseModel):
    # Live (untombstoned) memories in the namespace: the denominator of `fraction`.
    namespace_rows: int
    # One row per (actor, agent) that read the namespace in the window, most keys first.
    rows: list[_ExposureSummaryRow]


class MemoryOwnerReportInput(BaseModel):
    """The owner's view of their memory: what they own, who
    read it, how it was used. `user_id` is bound the way `read_scope=own_user`
    binds a search: a token pinned with `read_user_id` supplies it and refuses
    another; an unpinned token must name it. Namespaces are the token's
    readable set, narrowed by `namespace` when given."""

    org: str
    user_id: str | None = None
    namespace: str | None = None
    # Look-back window in hours for `reads`, 1 to 8760; `rows` and `usage` are all-time.
    since_hours: int = Field(default=720, ge=1, le=8760)
    # At most this many `reads` rows, most calls first; capped at 1000.
    limit: int = Field(default=50, ge=1, le=1000)


class _OwnerRowsRow(BaseModel):
    namespace: str
    memory_type: str
    n: int


class _OwnerReadsRow(BaseModel):
    namespace: str
    agent: str
    actor: str
    calls: int
    distinct_keys: int
    last_read: datetime


class _OwnerUsageRow(BaseModel):
    namespace: str
    recalls: int
    used: int
    harm: int
    last_recalled: datetime | None = None


class MemoryOwnerReportOutput(BaseModel):
    user_id: str
    # The `reads` window the report used, echoed from the request.
    since_hours: int
    # Active, untombstoned memories the owner has across the readable namespaces.
    live_rows: int
    # The owner's active memories counted per (namespace, memory_type).
    rows: list[_OwnerRowsRow]
    # Who read the owner's memories in the window: per (namespace, agent, actor), calls,
    # distinct keys and last read, most calls first.
    reads: list[_OwnerReadsRow]
    # Per namespace, the recall / used / harm totals of the owner's memories and the
    # last recall time.
    usage: list[_OwnerUsageRow]
