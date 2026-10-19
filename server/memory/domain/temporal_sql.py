"""One place for loom's point-in-time (`as_of`) WHERE fragments.

Two axes, two predicates, shared by the vector leg, the memory-table reads, the
summary legs and the records leg. A hand-rolled copy per leg drifts (an alias
shadowing the base column silently kills a leg; one leg prefers
`temporal_anchor` over `valid_at` while the others do the opposite). Build the
fragment here; bind `{as_of:DateTime}` at the call site.

valid_time — "what was TRUE at T". Lower bound only (the memory row has no
  interval semantics; supersession, not exclusion, disambiguates). The bound
  is the best world-axis date the row carries, in order of authority:
    valid_at        the writer said when the fact became true
    temporal_anchor the date the fact is ABOUT (event date; bulk imports carry
                    it even when valid_at is unknown)
    created_at      when loom wrote it — the ingest clock, a poor proxy: a
                    bulk backfill stamps months of history with one evening
  Written in OR-form, not coalesce, so the created_at index prune stays fast.

knowledge_time — "what loom KNEW at T". Record axis: written by T and not yet
  retired or deleted by T. A row superseded AFTER T was still believed at T
  and must come back; that is what `invalid_at` (retirement stamp) and
  `tombstoned_at` (deletion stamp) are compared against. A retired row with
  no stamp (an older supersession) is excluded — unknown retirement time is
  treated conservatively.
"""

from __future__ import annotations

# Status values a live (non-replay) read admits. Under knowledge_time a
# 'superseded' row is admitted when its retirement postdates the anchor.
LIVE_STATUSES: tuple[str, ...] = ("active", "duplicate", "conflicted")
# Status values a read SERVES by default: a `duplicate` row is
# live — its canonical answers for it — but it is not returned unless the
# caller asks (`include_duplicates`).
SERVED_STATUSES: tuple[str, ...] = ("active", "conflicted")
# A `superseded` row is retired, not live: a later write replaced it and
# `superseded_by` names the successor. It is served only when the caller asks
# (`include_superseded`) — to explain what a value was before and why it
# changed — never by default.
SUPERSEDED_STATUS = "superseded"
# A `proposed` row is a fact written for trial. It is served
# only to a read that opts in (`include_proposed`) — the run that tests it —
# and graded outcomes of such runs are what it is accepted or rejected on.
PROPOSED_STATUS = "proposed"


def served_statuses(
    *,
    include_duplicates: bool = False,
    include_superseded: bool = False,
    include_proposed: bool = False,
) -> tuple[str, ...]:
    """The status values one read admits: the served set, plus `duplicate`
    under `include_duplicates`, plus `superseded` under
    `include_superseded`, plus `proposed` under
    `include_proposed`."""
    statuses = LIVE_STATUSES if include_duplicates else SERVED_STATUSES
    if include_superseded:
        statuses = (*statuses, SUPERSEDED_STATUS)
    if include_proposed:
        statuses = (*statuses, PROPOSED_STATUS)
    return statuses


def served_status_sql(
    *,
    alias: str = "",
    include_duplicates: bool = False,
    include_superseded: bool = False,
    include_proposed: bool = False,
) -> str:
    """`status IN (...)` for a live read: the served statuses, widened by the
    caller's flags."""
    statuses = served_statuses(
        include_duplicates=include_duplicates,
        include_superseded=include_superseded,
        include_proposed=include_proposed,
    )
    return f"{_c(alias, 'status')} IN ({', '.join(repr(x) for x in statuses)})"


# An entity edge's holder: a `loom.memory` row live, not deleted, not expired and its
# validity not ended. A 'high' row counts: the only edges linked to it are those of the
# entities its writer declared (its value is never mined).
EDGE_HOLDER_SQL = (
    f"tombstoned_at IS NULL AND status IN ({', '.join(repr(s) for s in LIVE_STATUSES)})"
    " AND (expires_at IS NULL OR expires_at > now())"
    " AND (valid_to IS NULL OR valid_to > now())"
)

# A row whose current value may hold graph facts mined or stated from its text: an edge
# holder that is not 'high' (a 'high' row's content stays out of the shared graph). Every
# path that keeps or reopens a row's triples asks this; a point write asks its Python twin
# (`entity_projections.holds_graph_facts`) of the row it just wrote.
GRAPH_HOLDER_SQL = EDGE_HOLDER_SQL + " AND sensitivity != 'high'"

# A row that still holds the value it was written with, for the edges of its declared
# entities: not deleted and not retired (superseded, rejected or tombstoned), which keep
# the row's value, so the columns tell. No validity or expiry test, so a proposal or a
# fact dated in the past still counts.
EDGE_HOLDS_VALUE_SQL = (
    "tombstoned_at IS NULL"
    f" AND status IN ({', '.join(repr(s) for s in (*LIVE_STATUSES, PROPOSED_STATUS))})"
)

# The same for edges mined from the value: a row re-set as 'high' no longer holds them. A
# point write's late projection of mined entities fences on it.
HOLDS_VALUE_SQL = EDGE_HOLDS_VALUE_SQL + " AND sensitivity != 'high'"


def _c(alias: str, col: str) -> str:
    return f"{alias}.{col}" if alias else col


def as_of_predicate(
    basis: str = "valid_time",
    *,
    alias: str = "",
    param: str = "as_of",
    valid_col: str = "valid_at",
    anchor_col: str | None = "temporal_anchor",
    created_col: str = "created_at",
    invalid_col: str | None = None,
    tomb_col: str | None = None,
) -> str:
    """Parenthesised WHERE fragment; the caller binds `{param}` (DateTime).

    `anchor_col=None` for tables without an event-date column (summaries).
    `invalid_col` / `tomb_col` only matter for knowledge_time: pass them so a
    row retired or deleted after the anchor is still counted as known then.
    """
    t = f"{{{param}:DateTime}}"
    if basis == "knowledge_time":
        parts = [f"{_c(alias, created_col)} <= {t}"]
        if invalid_col:
            ic = _c(alias, invalid_col)
            parts.append(f"({ic} IS NULL OR {ic} > {t})")
        if tomb_col:
            tc = _c(alias, tomb_col)
            parts.append(f"({tc} IS NULL OR {tc} > {t})")
        return "(" + " AND ".join(parts) + ")"
    v, c = _c(alias, valid_col), _c(alias, created_col)
    if anchor_col:
        a = _c(alias, anchor_col)
        return (
            f"(({v} IS NOT NULL AND {v} <= {t}) "
            f"OR ({v} IS NULL AND {a} IS NOT NULL AND {a} <= {t}) "
            f"OR ({v} IS NULL AND {a} IS NULL AND {c} <= {t}))"
        )
    return f"(({v} IS NOT NULL AND {v} <= {t}) OR ({v} IS NULL AND {c} <= {t}))"


def status_predicate(
    basis: str = "valid_time",
    *,
    alias: str = "",
    param: str = "as_of",
    replaying: bool = False,
    status_col: str = "status",
    invalid_col: str = "invalid_at",
    include_duplicates: bool = False,
    include_superseded: bool = False,
    include_proposed: bool = False,
) -> str:
    """Which rows a read admits. Live reads and valid_time replay: the served statuses —
    `duplicate` rows only under `include_duplicates`, `superseded` rows only under
    `include_superseded`. knowledge_time replay additionally admits a row superseded AFTER
    the anchor — loom still believed it then."""
    s = _c(alias, status_col)
    statuses = served_statuses(
        include_duplicates=include_duplicates,
        include_superseded=include_superseded,
        include_proposed=include_proposed,
    )
    live = ", ".join(f"'{x}'" for x in statuses)
    base = f"{s} IN ({live})"
    if include_superseded or not (replaying and basis == "knowledge_time"):
        return base
    ic = _c(alias, invalid_col)
    return f"({base} OR ({s} = 'superseded' AND {ic} IS NOT NULL AND {ic} > {{{param}:DateTime}}))"


def tombstone_predicate(
    basis: str = "valid_time",
    *,
    alias: str = "",
    param: str = "as_of",
    replaying: bool = False,
    tomb_col: str = "tombstoned_at",
) -> str:
    """`tombstoned_at IS NULL` — except under knowledge_time replay, where a
    row deleted AFTER the anchor was still known then."""
    tc = _c(alias, tomb_col)
    if not (replaying and basis == "knowledge_time"):
        return f"{tc} IS NULL"
    return f"({tc} IS NULL OR {tc} > {{{param}:DateTime}})"
