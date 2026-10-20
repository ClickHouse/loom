"""The sensitivity sweep's two reads of `loom.memory`: a page's sort keys, then the current
version of each of them (the paging loop is `memory.app.sensitivity_sweep`).
"""

from __future__ import annotations

# loom.memory's sort key: a scan pages on it so each page starts where the last one ended.
SORT_KEY = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")

# The text the coarse filter matches: an object value's JSON, any other value's text.
_SCAN_TEXT = "if(length(toString(value_json)) > 2, toString(value_json), value)"


def _after_last_key() -> str:
    """The sort key past `{after_<column>}`, as `a > x OR (a = x AND (...))`."""
    cond = ""
    for col in reversed(SORT_KEY):
        param = f"{{after_{col}:String}}"
        if cond:
            cond = f"{col} > {param} OR ({col} = {param} AND ({cond}))"
        else:
            cond = f"{col} > {param}"
    return cond


def _where(*, org: str | None, namespace: str | None, since_days: int | None) -> str:
    """The conditions a scanned row meets, each on one row version."""
    where = f"""tombstoned_at IS NULL AND sensitivity = ''
          AND (expires_at IS NULL OR expires_at > now())
          AND match({_SCAN_TEXT}, {{shape:String}})"""
    if org:
        where += " AND org = {org:String}"
    if namespace:
        where += " AND namespace = {ns:String}"
    if since_days:
        where += " AND created_at > now() - INTERVAL {days:UInt32} DAY"
    return where


def keys_sql(
    *, org: str | None, namespace: str | None, since_days: int | None, after: bool = False
) -> str:
    """The sort keys of one page, `{lim}` distinct keys read without FINAL: any row version
    that meets the conditions gives its key, so the page's keys include every key whose
    current version meets them. Without FINAL the window prunes partitions, every thread
    reads, and a read in key order stops at the LIMIT."""
    where = _where(org=org, namespace=namespace, since_days=since_days)
    if after:  # the next page, after the last key of the one before
        where += f" AND ({_after_last_key()})"
    return f"""
        SELECT DISTINCT {", ".join(SORT_KEY)}
        FROM loom.memory
        WHERE {where}
        ORDER BY {", ".join(SORT_KEY)}
        LIMIT {{lim:UInt32}}
    """


def scan_sql(
    *, org: str | None, namespace: str | None, since_days: int | None, after: bool = False
) -> str:
    """The current version of each of the page's keys that still meets the conditions. The
    keys come from `keys_sql` as a subquery, so the statement binds no key list and the
    FINAL read touches only the granules of the page's keys."""
    where = _where(org=org, namespace=namespace, since_days=since_days)
    keys = keys_sql(org=org, namespace=namespace, since_days=since_days, after=after)
    return f"""
        SELECT memory_id, org, namespace, agent, user_id, session_id, memory_key,
               {_SCAN_TEXT} AS value
        FROM loom.memory FINAL
        WHERE {where}
          AND ({", ".join(SORT_KEY)}) IN ({keys})
        ORDER BY {", ".join(SORT_KEY)}
    """


__all__ = ["SORT_KEY", "keys_sql", "scan_sql"]
