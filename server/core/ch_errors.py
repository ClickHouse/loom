"""Which driver errors `CHClient` retries, and how.

A transient network failure is retried on a reconnect; an error that is a
property of the replica that answered is retried on a fresh connection so the
load balancer can pick another; a replica that rejects the quantized-codes
setting mid-upgrade is retried without it. Everything else is re-raised.
"""

from __future__ import annotations

import re

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
    try:
        from clickhouse_connect.driver.exceptions import OperationalError
    except Exception:  # noqa: BLE001 — driver missing in some test contexts
        return False
    return isinstance(e, OperationalError) and _NETWORK_ERROR_MARK in str(e)


# Server-side errors that are a property of the REPLICA that answered, not of
# the query: retried once on a fresh connection so the load balancer can hand
# the request to another replica. During a rolling Cloud upgrade an older
# replica can fail a query (e.g. `NOT_FOUND_COLUMN_IN_BLOCK` on the text-index
# leg of bm25_hybrid search under FINAL) that succeeds on the next connection.
# A code that is
# usually a genuine SQL bug is retried exactly once and re-raised if it
# repeats, so a real bug still surfaces — one round-trip later.
# 10  NOT_FOUND_COLUMN_IN_BLOCK — a 26.6 replica's FINAL+PREWHERE bug
# 242 TABLE_IS_READ_ONLY — one replica lost its Keeper session; the others serve
# 999 KEEPER_EXCEPTION "Session expired" — same event, seen from the query side
_REPLICA_BOUND_ERROR_CODES: tuple[int, ...] = (10, 242, 999)
# How many fresh connections to try after a replica-bound error. Cloud puts a
# load balancer in front of the replicas, so one reconnect can land on the
# same replica that just failed, so a single retry is not enough.
REPLICA_BOUND_RETRIES: int = 2
# Strict-mode KeeperMap answers, the same on every replica.
_KEEPER_ANSWERS: tuple[str, ...] = ("Node exists", "Bad version")
# A strict-mode write on a sign-in claim another request just dropped
# (server/auth/claims.py): a lost race, not a replica problem.
_CLAIM_RACE: tuple[str, ...] = ("Transaction failed (No node)", "/oauth_claims/")


# A query-level setting one replica does not know. ClickHouse
# Cloud upgrades a service replica by replica, so for a while `system.settings`
# on one node lists `vector_search_use_quantized_codes` (26.7+) while another
# node still rejects it with code 115 ("neither a builtin setting nor started
# with the prefix 'custom_'"). The boot probe asks one replica
# once; a process whose pooled connection later lands on the older replica
# then fails every vector read.
QUANTIZED_SETTING = "vector_search_use_quantized_codes"
_QUANTIZED_FRAGMENT_RE = re.compile(r",\s*vector_search_use_quantized_codes\s*=\s*1")


def is_unknown_quantized_setting(e: BaseException) -> bool:
    try:
        from clickhouse_connect.driver.exceptions import DatabaseError
    except Exception:  # noqa: BLE001 — an error classifier never raises
        return False
    if not isinstance(e, DatabaseError):
        return False
    msg = str(e)
    return "Code: 115." in msg and QUANTIZED_SETTING in msg


def strip_quantized_setting(sql: str) -> str:
    """The same query without the `vector_search_use_quantized_codes = 1`
    fragment `vector_search_settings()` appended; unchanged when absent."""
    return _QUANTIZED_FRAGMENT_RE.sub("", sql)


_SERVER_CODE = re.compile(r"Code: (\d+)\.")


def error_code(e: BaseException | str) -> int | None:
    """The ClickHouse server error code an exception (or its text) carries as
    `Code: <n>.`, or None."""
    m = _SERVER_CODE.search(str(e))
    return int(m.group(1)) if m else None


def is_replica_bound_error(e: BaseException) -> bool:
    try:
        from clickhouse_connect.driver.exceptions import DatabaseError
    except Exception:  # noqa: BLE001 — driver missing in some test contexts
        return False
    if not isinstance(e, DatabaseError):
        return False
    msg = str(e)
    if any(answer in msg for answer in _KEEPER_ANSWERS) or all(p in msg for p in _CLAIM_RACE):
        return False
    return any(f"Code: {code}." in msg for code in _REPLICA_BOUND_ERROR_CODES)


__all__ = [
    "QUANTIZED_SETTING",
    "REPLICA_BOUND_RETRIES",
    "error_code",
    "is_replica_bound_error",
    "is_transient_network_error",
    "is_unknown_quantized_setting",
    "strip_quantized_setting",
]
