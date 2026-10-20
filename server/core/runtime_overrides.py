"""loom.runtime_overrides: limits and settings an operator sets at runtime through
`/v1/admin/ratelimit.set`, `/v1/admin/budget.set` and `/v1/admin/settings.set`.

The admin route writes the row, then applies it in its own process; every
process re-reads the table every `runtime_overrides_refresh_seconds`, so an
override binds the whole deployment within that interval and outlives a
restart or a move to another host.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from server.core.ch_errors import error_code
from server.core.errors import BackendUnavailableError, LoomServerError
from server.core.repo_base import _CHClient, _default_client

logger = logging.getLogger("loom.runtime_overrides")

TABLE = "loom.runtime_overrides"
# key (namespace, op_class) / (org, op_class) → {"capacity": float, "rate": float}
RATELIMIT_NAMESPACE = "ratelimit.namespace"
RATELIMIT_ORG = "ratelimit.org"
# key (op,) / (consumer, op) → milliseconds, or "<n>x"
BUDGET_OP = "budget.op"
BUDGET_CONSUMER = "budget.consumer"
# key (field,) → the value of a LIVE setting (server.core.settings_overrides)
SETTING = "setting"

Snapshot = dict[str, dict[tuple[str, ...], Any]]

# An own write a read never shows (lost with a replica) stops overlaying after this
# (this process's monotonic clock).
_OWN_HOLD_NS = 300 * 10**9

# This process's own writes, (kind, key) → (value or None for cleared, row version,
# monotonic ns when written). A read may not show one yet (it began first, or a lagging
# replica answered); `load` lays it over what it read until a read shows that version or
# a newer one, so a refresh never undoes the override this process just set.
_own: dict[tuple[str, tuple[str, ...]], tuple[Any, int, int]] = {}
# The last version this process wrote: two of its writes never tie.
_last_version = 0

# A row's version is the ClickHouse server's clock, so writes made through different
# hosts order as they happened, whatever the hosts' own clocks say.
_VERSION_SQL = "SELECT toUnixTimestamp64Nano(now64(9)) AS v"

# Server refusals that repeat on every retry: no such column or table, a read-only
# user, no privilege.
_REFUSED_CODES = frozenset({16, 47, 60, 164, 497})

# Rows already reported as unparsable: said once per row, not on every refresh.
_unparsed: set[tuple[str, str, str]] = set()


class OverrideNotStoredError(LoomServerError):
    """409 — ClickHouse refused to store the override (a read-only user, for
    example), so no process changed; the server log has the reason. Not a 503:
    the refusal repeats until the deployment's ClickHouse user changes."""

    code = "OVERRIDE_NOT_STORED"
    status_code = 409


async def put(kind: str, key: tuple[str, ...], value: Any, *, ch: _CHClient | None = None) -> None:
    version = await _write(kind, key, json.dumps(value), deleted=0, ch=ch)
    _own[(kind, key)] = (value, version, time.monotonic_ns())


async def clear(kind: str, key: tuple[str, ...], *, ch: _CHClient | None = None) -> None:
    version = await _write(kind, key, "null", deleted=1, ch=ch)
    _own[(kind, key)] = (None, version, time.monotonic_ns())


async def _write(
    kind: str, key: tuple[str, ...], value: str, *, deleted: int, ch: _CHClient | None
) -> int:
    """Store one row; returns its version. A refused write raises `OverrideNotStoredError`,
    one a retry may store `BackendUnavailableError`."""
    global _last_version
    client = ch or _default_client()
    row = {"kind": kind, "key": json.dumps(list(key)), "value": value, "deleted": deleted}
    try:
        stamp = await client.query_one(_VERSION_SQL)
        version = max(int((stamp or {})["v"]), _last_version + 1)
        await client.insert(TABLE, [{**row, "version": version}], settings={"async_insert": 0})
    except LoomServerError:
        raise  # already typed: unreachable or timed out
    except Exception as e:  # a refusal is the caller's 409, anything else a 503
        logger.warning("runtime override %s %s not stored: %s", kind, key, e)
        if error_code(e) in _REFUSED_CODES:
            raise OverrideNotStoredError(
                "the override was not stored: ClickHouse refused the write to "
                f"{TABLE}; no process changed"
            ) from e
        raise BackendUnavailableError(
            f"the override was not stored ({TABLE} write failed); no process changed",
            operation="runtime_overrides.write",
        ) from e
    _last_version = version
    return version


async def load(*, ch: _CHClient | None = None) -> Snapshot:
    """kind → key → value, cleared keys left out. A row that does not parse is
    skipped, the rest adopted."""
    rows = await (ch or _default_client()).query_all(
        f"SELECT kind, key, value, deleted, version FROM {TABLE} FINAL"
    )
    out: Snapshot = {}
    seen: dict[tuple[str, tuple[str, ...]], int] = {}
    for r in rows:
        try:
            kind, key = str(r["kind"]), tuple(json.loads(r["key"]))
            version, deleted = int(r["version"]), bool(int(r["deleted"]))
            value = None if deleted else json.loads(r["value"])
        except (KeyError, TypeError, ValueError) as e:
            _warn_unparsed(r, e)
            continue
        seen[(kind, key)] = version
        if not deleted:
            out.setdefault(kind, {})[key] = value
    now = time.monotonic_ns()
    for (kind, key), (value, version, written) in list(_own.items()):
        if seen.get((kind, key), -1) >= version or now - written > _OWN_HOLD_NS:
            del _own[(kind, key)]  # the read shows it (or a newer write)
        elif value is None:
            out.get(kind, {}).pop(key, None)
        else:
            out.setdefault(kind, {})[key] = value
    return out


def _warn_unparsed(r: dict[str, Any], e: Exception) -> None:
    ident = (str(r.get("kind")), str(r.get("key")), str(r.get("version")))
    if ident in _unparsed:
        return
    if len(_unparsed) >= 10_000:
        _unparsed.clear()
    _unparsed.add(ident)
    logger.warning("runtime override %s %s does not parse (%s); skipped", ident[0], ident[1], e)
