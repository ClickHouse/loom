"""Which driver errors `CHClient` retries, and how; and how a ClickHouse error reads
to a caller that must tell the server's state from a refusal of its rows.

A transient network failure is retried on a reconnect; an error that is a
property of the replica that answered is retried on a fresh connection so the
load balancer can pick another; a replica that rejects the quantized-codes
setting mid-upgrade is retried without it. Everything else is re-raised.
"""

from __future__ import annotations

import re

from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from server.core.errors import BackendUnavailableError, OverloadedError

# Transient connection failures worth a reconnect-retry. NOT ClickHouseError:
# semantic errors (syntax/permission/type) won't fix on retry. The aiohttp
# backend wraps every transport failure (a peer close, a reset, its own read timeout) into
# `OperationalError("Network Error: …")`, which `is_transient_network_error`
# recognises. `TimeoutError` is deliberately absent: the only one that reaches
# this layer is loom's own class deadline, and a statement
# that hung once is not re-run for another deadline.
_RECONNECTABLE_EXCEPTIONS = (
    ConnectionError,
    OSError,
)
_NETWORK_ERROR_MARK = "Network Error"
# A coded vector column refuses a vector of another width with this code
# (SIZES_OF_ARRAYS_DONT_MATCH); running the same INSERT again cannot pass.
WIDTH_MISMATCH_CODE = 190


def is_transient_network_error(e: BaseException) -> bool:
    """A dropped, reset or peer-closed connection, before or after the request
    was sent — the driver cannot tell which, and neither can loom."""
    if isinstance(e, _RECONNECTABLE_EXCEPTIONS) and not isinstance(e, TimeoutError):
        return True
    return isinstance(e, OperationalError) and _NETWORK_ERROR_MARK in str(e)


# Server-side errors that are a property of the REPLICA that answered, not of
# the query: retried on a fresh connection, `ch_replica_bound_retries` times, so
# the load balancer can hand the request to another replica. During a rolling
# Cloud upgrade an older replica can fail a query (e.g. `NOT_FOUND_COLUMN_IN_BLOCK`
# on the text-index leg of bm25_hybrid search under FINAL) that succeeds on the
# next connection. A code that is usually a genuine SQL bug is retried as many
# times and re-raised if it repeats, so a real bug still surfaces, a few
# round-trips later.
# 10  NOT_FOUND_COLUMN_IN_BLOCK — a 26.6 replica's FINAL+PREWHERE bug
# 242 TABLE_IS_READ_ONLY — one replica lost its Keeper session; the others serve
# 999 KEEPER_EXCEPTION "Session expired" — same event, seen from the query side
_REPLICA_BOUND_ERROR_CODES: tuple[int, ...] = (10, 242, 999)
# Strict-mode KeeperMap answers, the same on every replica.
_KEEPER_ANSWERS: tuple[str, ...] = ("Node exists", "Bad version")
# A strict-mode KeeperMap write on a key another request just dropped (each
# key is a node under <table root>/data/): a lost race, not a replica problem.
_KEEPER_MAP_RACE: tuple[str, ...] = ("Transaction failed (No node)", "/data/")


# A query-level setting one replica does not know. ClickHouse
# Cloud upgrades a service replica by replica, so for a while `system.settings`
# on one node lists `vector_search_use_quantized_codes` (26.7+) while another
# node still rejects it with code 115 ("neither a builtin setting nor started
# with the prefix 'custom_'"). The boot probe asks one replica
# once; a process whose pooled connection later lands on the older replica
# then fails every vector read.
QUANTIZED_SETTING = "vector_search_use_quantized_codes"
_QUANTIZED_FRAGMENT_RE = re.compile(r",\s*vector_search_use_quantized_codes\s*=\s*1")


# Server states that clear on their own; the same insert lands once they do.
# 202 TOO_MANY_SIMULTANEOUS_QUERIES, 225 NO_ZOOKEEPER, 242 TABLE_IS_READ_ONLY,
# 252 TOO_MANY_PARTS (a merge backlog), 319 UNKNOWN_STATUS_OF_INSERT, 999 KEEPER_EXCEPTION
_SERVER_STATE_CODES: tuple[int, ...] = (202, 225, 242, 252, 319, 999)
# Code 252 for an INSERT block spanning more partitions than the server allows: the rows'
# own refusal, which a smaller batch avoids.
_ROWS_SPAN_PARTITIONS = "Too many partitions for single INSERT block"
# 241 MEMORY_LIMIT_EXCEEDED against the server's whole memory or the user's, which other
# queries share, not the query's own limit: "(total)", or the user's as "(for user)" in
# older servers' text and "User memory limit exceeded" in current ones (26.5 and later).
_SHARED_MEMORY = ("(total)", "(for user)", "user memory limit exceeded")


def is_shared_memory_limit(e: BaseException) -> bool:
    """ClickHouse ran short of the server's or the user's memory, which other queries
    share: a state that passes, not a refusal of this statement."""
    return (
        isinstance(e, DatabaseError)
        and error_code(e) == 241
        and any(scope in str(e).lower() for scope in _SHARED_MEMORY)
    )


def is_passing_state(e: BaseException) -> bool:
    """ClickHouse answered with a state of its own that clears by itself (a merge
    backlog, Keeper lost, too many queries), or every pooled connection was busy: the
    rows were not refused, and the same insert lands later."""
    if isinstance(e, OverloadedError) or is_shared_memory_limit(e):
        return True
    return (
        isinstance(e, DatabaseError)
        and error_code(e) in _SERVER_STATE_CODES
        and _ROWS_SPAN_PARTITIONS not in str(e)
    )


def is_connection_failure(e: BaseException) -> bool:
    """No answer from ClickHouse to the request: the client's reconnect gave up
    (`BackendUnavailableError`), or the driver's connection dropped, timed out or met a
    proxy's own status (`OperationalError`). Whether the server or the request is at
    fault only a probe of the server tells."""
    return isinstance(e, BackendUnavailableError | OperationalError)


async def clickhouse_answers() -> bool:
    """ClickHouse answers a trivial query now."""
    from server.core.ch import get_client

    try:
        return await get_client().query_one("SELECT 1 AS ok") is not None
    except Exception:  # noqa: BLE001 — a probe that fails is its answer
        return False


async def refused_nothing(error: BaseException, *, probe: bool) -> bool:
    """ClickHouse did not refuse the rows: it answered with a state that clears by
    itself, or it does not answer at all. A connection that failed while ClickHouse
    answers a probe is the request's own: a refusal. Without the probe, no connection
    failure is."""
    if is_passing_state(error):
        return True
    if not is_connection_failure(error):
        return False
    return not probe or not await clickhouse_answers()


def is_unknown_quantized_setting(e: BaseException) -> bool:
    if not isinstance(e, DatabaseError):
        return False
    msg = str(e)
    return "Code: 115." in msg and QUANTIZED_SETTING in msg


def strip_quantized_setting(sql: str) -> str:
    """The same query without the `vector_search_use_quantized_codes = 1`
    fragment `vector_search_settings()` appended; unchanged when absent."""
    return _QUANTIZED_FRAGMENT_RE.sub("", sql)


_SERVER_CODE = re.compile(r"Code: (\d+)\.")


_DRIVER_PREFIX = re.compile(r"^Received ClickHouse exception, code: \d+, server response: ")


def error_line(e: BaseException, limit: int = 200) -> str:
    """The first line of an error a person can read: without the driver's
    prefix and the URL it appends, or the exception's type when it has no text."""
    lines = str(e).splitlines()
    if not lines:
        return type(e).__name__
    return _DRIVER_PREFIX.sub("", lines[0]).split(" (for url")[0][:limit]


def error_code(e: BaseException | str) -> int | None:
    """The ClickHouse server error code an exception (or its text) carries as
    `Code: <n>.`, or None."""
    m = _SERVER_CODE.search(str(e))
    return int(m.group(1)) if m else None


def is_replica_bound_error(e: BaseException) -> bool:
    if not isinstance(e, DatabaseError):
        return False
    msg = str(e)
    if any(answer in msg for answer in _KEEPER_ANSWERS) or all(p in msg for p in _KEEPER_MAP_RACE):
        return False
    return any(f"Code: {code}." in msg for code in _REPLICA_BOUND_ERROR_CODES)


__all__ = [
    "QUANTIZED_SETTING",
    "error_code",
    "is_connection_failure",
    "is_passing_state",
    "is_shared_memory_limit",
    "is_replica_bound_error",
    "is_transient_network_error",
    "is_unknown_quantized_setting",
    "strip_quantized_setting",
]
