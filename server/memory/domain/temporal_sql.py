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

`as_of` is the replay with the bi-temporal journal off (`memory_bitemporal_enabled`);
with it on, a point-in-time read of memory goes through the journal
(server/memory/app/temporal_reads.py), and the graph and records planes use the
`valid_as_of` / `known_as_of` interval tests at the end of this module.
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


def graph_validity_predicate(*, valid: bool, known: bool, alias: str = "") -> str:
    """The entity graph's interval test (both bounds inclusive, as the triple plane
    always read them) at `{valid_as_of:DateTime}` or now, and with `known` the knowledge
    axis at `{known_as_of:DateTime}`: a row recorded after K is not served, and a
    close recorded after K (`closed_at`) is not applied yet. With `known` and no
    `valid`, the world is read at K."""
    v = "{valid_as_of:DateTime}" if valid else ("{known_as_of:DateTime}" if known else "now()")
    vf, vt = _c(alias, "valid_from"), _c(alias, "valid_to")
    open_end = f"{vt} IS NULL OR {vt} >= {v}"
    parts = []
    if valid or known:
        parts.append(f"({vf} IS NULL OR {vf} <= {v})")
    if known:
        k = "{known_as_of:DateTime}"
        open_end += f" OR ({_c(alias, 'closed_at')} IS NOT NULL AND {_c(alias, 'closed_at')} > {k})"
        parts.append(f"{_c(alias, 'recorded_at')} <= {k}")
    parts.append(f"({open_end})")
    return " AND ".join(parts)


def record_validity_predicate(alias: str = "", param: str = "valid_as_of") -> str:
    """A record's validity at `{param}`: the half-open `[valid_at, valid_to)`, so a
    record superseded at T (its `valid_to` is the successor's `valid_at`) and its
    successor never both hold at T."""
    t = f"{{{param}:DateTime}}"
    va, vt = _c(alias, "valid_at"), _c(alias, "valid_to")
    return f"({va} IS NULL OR {va} <= {t}) AND ({vt} IS NULL OR {vt} > {t})"
