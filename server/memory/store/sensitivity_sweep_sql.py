"""The sensitivity sweep's two reads of `loom.memory`: a page's sort keys, then the current
version of each of them (the paging loop is `memory.app.sensitivity_sweep`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# loom.memory's sort key: a scan pages on it so each page starts where the last one ended.
SORT_KEY = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")


# The sort key's first columns, which name an agent.
AGENT_KEY = SORT_KEY[:3]


def past_agent(key: dict[str, Any]) -> dict[str, Any]:
    """The place past every key in `key`'s agent: its agent columns alone. A read bounds on
    that shorter tuple, so it skips no stored value, whatever bytes the value holds."""
    return {col: key[col] for col in AGENT_KEY}


# The texts the coarse filter matches: the bytes the writer stored in `value`, where a
# dotted key keeps its words (`{"security.code": ...}`), and an object value as ClickHouse
# stores it, which an import may fill with more than `value` holds.
_VALUE_JSON = "toString(value_json)"


def _key_bound(prefix: str, columns: Sequence[str], op: str, last_op: str) -> str:
    """The tuple of `columns` against `{<prefix>_<column>}`, as `a op x OR (a = x AND (...))`,
    with `last_op` on the last column."""
    cond = ""
    for col in reversed(columns):
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
          AND (match(value, {{shape:String}}) OR match({_VALUE_JSON}, {{shape:String}}))"""
    if org:
        where += " AND org = {org:String}"
    if namespace:
        where += " AND namespace = {ns:String}"
    if since_days:
        where += " AND created_at > now() - INTERVAL {days:UInt32} DAY"
    return where


def keys_sql(
    *,
    org: str | None,
    namespace: str | None,
    since_days: int | None,
    after: Sequence[str] = (),
    stop: Sequence[str] = (),
) -> str:
    """The sort keys of one page, `{lim}` distinct keys read without FINAL: any row version
    that meets the conditions gives its key, so the page's keys include every key whose
    current version meets them. Without FINAL the window prunes partitions, every thread
    reads, and a read in key order stops at the LIMIT.

    `after` and `stop` name the columns of a bound, a prefix of the sort key: only keys past
    `{after_<column>}` on those columns, and with `stop`, only keys up to and including
    `{stop_<column>}` on its columns. A bound on the whole sort key is one key; a shorter one
    is the place past (or up to the end of) every key that shares its values."""
    where = _where(org=org, namespace=namespace, since_days=since_days)
    if after:  # the next page, after the last key of the one before
        where += f" AND ({_key_bound('after', after, '>', '>')})"
    if stop:  # a pass that went back to the first key reads up to where it started
        where += f" AND ({_key_bound('stop', stop, '<', '<=')})"
    return f"""
        SELECT DISTINCT {", ".join(SORT_KEY)}
        FROM loom.memory
        WHERE {where}
        ORDER BY {", ".join(SORT_KEY)}
        LIMIT {{lim:UInt32}}
    """


def scan_sql(
    *,
    org: str | None,
    namespace: str | None,
    since_days: int | None,
    after: Sequence[str] = (),
) -> str:
    """The current version of each key past `{after_<column>}` on the columns `after` names
    (as in `keys_sql`) up to the page's last key `{upto_<column>}` that still meets the
    conditions. The keys are a subquery, so the statement binds no key list and the FINAL
    read touches only the granules of the page's keys. The subquery takes the range, not
    `keys_sql` again: a key written into the range between the two reads is read too, and
    a LIMIT cannot push the page's last key out or pull the next page's first key in."""
    where = _where(org=org, namespace=namespace, since_days=since_days)
    in_range = f"{where} AND ({_key_bound('upto', SORT_KEY, '<', '<=')})"
    if after:
        in_range += f" AND ({_key_bound('after', after, '>', '>')})"
    return f"""
        SELECT memory_id, org, namespace, agent, user_id, session_id, memory_key, version,
               value, {_VALUE_JSON} AS object_text
        FROM loom.memory FINAL
        WHERE {where}
          AND ({", ".join(SORT_KEY)}) IN (
            SELECT {", ".join(SORT_KEY)} FROM loom.memory WHERE {in_range}
          )
        ORDER BY {", ".join(SORT_KEY)}
    """


__all__ = ["AGENT_KEY", "SORT_KEY", "keys_sql", "past_agent", "scan_sql"]
