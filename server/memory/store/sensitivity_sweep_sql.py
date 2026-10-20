"""The sensitivity sweep's two reads of `loom.memory`: a page's sort keys, then the current
version of each key in that page's range (the paging loop is `memory.app.sensitivity_sweep`).
"""

from __future__ import annotations

# loom.memory's sort key: a scan pages on it so each page starts where the last one ended.
SORT_KEY = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")

# The text the coarse filter matches: an object value's JSON, any other value's text.
_SCAN_TEXT = "if(length(toString(value_json)) > 2, toString(value_json), value)"


def _key_bound(prefix: str, op: str, last_op: str) -> str:
    """A sort key bound on `{<prefix>_<column>}`, as `a op x OR (a = x AND (...))`, with
    `last_op` on the last column."""
    cond = ""
    for col in reversed(SORT_KEY):
        param = f"{{{prefix}_{col}:String}}"
        if cond:
            cond = f"{col} {op} {param} OR ({col} = {param} AND ({cond}))"
        else:
            cond = f"{col} {last_op} {param}"
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
    """The sort keys of one page, read without FINAL: any row version that meets the
    conditions gives its key, so the page's keys include every key whose current version
    meets them. Without FINAL the window prunes partitions, every thread reads, and a read
    in key order stops at the LIMIT."""
    where = _where(org=org, namespace=namespace, since_days=since_days)
    if after:  # the next page, after the last key of the one before
        where += f" AND ({_key_bound('after', '>', '>')})"
    return f"""
        SELECT {", ".join(SORT_KEY)}
        FROM loom.memory
        WHERE {where}
        ORDER BY {", ".join(SORT_KEY)}
        LIMIT {{lim:UInt32}}
    """


def scan_sql(
    *, org: str | None, namespace: str | None, since_days: int | None, after: bool = False
) -> str:
    """The current version of each key in one page's range that still meets the conditions:
    after the last key of the page before and up to `{upto_<column>}`, this page's last key.
    Every key whose current version meets them is in the page, so the range holds the same
    rows as the page's key list, in at most twelve key parameters whatever the page size."""
    where = _where(org=org, namespace=namespace, since_days=since_days)
    if after:
        where += f" AND ({_key_bound('after', '>', '>')})"
    where += f" AND ({_key_bound('upto', '<', '<=')})"
    return f"""
        SELECT memory_id, org, namespace, agent, user_id, session_id, memory_key,
               {_SCAN_TEXT} AS value
        FROM loom.memory FINAL
        WHERE {where}
        ORDER BY {", ".join(SORT_KEY)}
    """


__all__ = ["SORT_KEY", "keys_sql", "scan_sql"]
