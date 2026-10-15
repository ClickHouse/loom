"""The served-status and validity WHERE fragments shared by the memory legs.

Which statuses a read serves, and the valid-time interval tests of the graph and
the records plane. A point-in-time read of memory goes through the bi-temporal
journal (server/memory/app/temporal_reads.py), not through these.
"""

from __future__ import annotations

# Status values a live read admits.
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
