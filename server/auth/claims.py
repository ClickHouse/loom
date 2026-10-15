"""One sign-in state transition at a time per grant.

A transition (spending a code, refresh token or device code; signing in on a
device grant; approving one) reads the grant's row in loom.oauth_state, checks
it and writes the next version. Two requests doing that at once could both
pass the check, so each transition holds the grant's key in loom.oauth_claims
(KeeperMap, schemas/migrations/029_oauth_claims.sql) for its read and write:
Keeper refuses a second insert of a key under keeper_map_strict_mode, so of
two concurrent transitions exactly one runs and the other waits or is told to
retry. The key is dropped when the transition is done; the written row keeps
the state from then on. Each claim names a random holder. A key older than
`_STALE_S` belongs to a holder that died, and a waiter takes it over by
rewriting the holder. Takeover and release are both versioned writes in strict
mode, so of a late release and a takeover only one lands.

A live holder must be done before its claim can go stale. Every statement it
issues is bounded at `_STATEMENT_S`, and its write must start by `_HOLD_S`
(`before_write`); a write is never cancelled once sent, since ClickHouse may
still apply it. So the last write, with its retries, ends long before
`_STALE_S`.

A ClickHouse without a Keeper has no claims table: transitions then run
unclaimed, as they did before, and /v1/health says so
(`oauth_transitions_not_atomic`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from server.core.ch import statement_timeout
from server.core.schema.migrate_lease import is_keeper_table, key_exists

log = logging.getLogger(__name__)

_TABLE = "loom.oauth_claims"
_TABLE_SQL = "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'oauth_claims'"
_STALE_S = 60  # a claim this old belongs to a holder that died
_HOLD_S = 10  # a holder's write must start by then
_STATEMENT_S = 5  # each statement's bound; a write retried twice still ends by 25 s
_WAITS_S = (0.05, 0.1, 0.2, 0.4)  # then the request is told to retry

# Whether loom.oauth_claims exists here: None until the boot probe or the
# first transition asks.
_atomic: bool | None = None

# When the claim held by this task stops covering a write.
_deadline: ContextVar[float | None] = ContextVar("oauth_claim_deadline", default=None)


class Busy(Exception):
    """Another request is moving this grant right now."""


def before_write() -> None:
    """Call before a claimed transition writes. Past its holder's time the
    claim may be taken over, so nothing is written and the request retries."""
    deadline = _deadline.get()
    if deadline is not None and asyncio.get_running_loop().time() > deadline:
        raise Busy("the claim was held too long to write under")


def transitions_atomic() -> bool | None:
    """Whether transitions are claimed in Keeper here; None = not asked yet."""
    return _atomic


async def _available(ch: Any) -> bool:
    global _atomic
    if _atomic is None:
        _atomic = is_keeper_table(await ch.query_one(_TABLE_SQL))
        if not _atomic:
            log.warning("sign-in: no %s (no Keeper); transitions are not claimed", _TABLE)
    return _atomic


async def probe(ch: Any) -> None:
    """At boot, so /v1/health names the missing table before any sign-in."""
    try:
        await _available(ch)
    except Exception as e:  # noqa: BLE001 — a failed probe leaves it to the first transition
        log.warning("oauth claims: could not read system.tables: %s", e)


def _lost_race(e: BaseException) -> bool:
    """Keeper's answer to a versioned write on a key someone else changed."""
    return key_exists(e) or "Bad version" in str(e) or "No node" in str(e)


async def _claim(ch: Any, key: str, holder: str) -> bool:
    """Insert the key, or find it already ours: a retried insert whose first
    answer was lost, or a takeover that landed."""
    try:
        await ch.execute(
            f"INSERT INTO {_TABLE} (claim_key, holder, claimed_at) "
            "SETTINGS keeper_map_strict_mode = 1 VALUES ({k:String}, {h:String}, now64(3))",
            params={"k": key, "h": holder},
        )
        return True
    except Exception as e:  # a taken key is the answer; Keeper failing is a retry
        if not key_exists(e):
            log.warning("oauth claims: Keeper refused %s: %s", key, e)
            raise Busy(key) from e
    try:
        row = await ch.query_one(
            f"SELECT holder FROM {_TABLE} WHERE claim_key = {{k:String}}", params={"k": key}
        )
    except Exception as e:  # Keeper unreadable: the request retries
        raise Busy(key) from e
    return bool(row) and row["holder"] == holder


async def _take_over_if_stale(ch: Any, key: str, holder: str) -> None:
    try:
        await ch.execute(
            f"ALTER TABLE {_TABLE} UPDATE holder = {{h:String}}, claimed_at = now64(3) "
            "WHERE claim_key = {k:String} "
            f"AND claimed_at < now64(3) - INTERVAL {_STALE_S} SECOND "
            "SETTINGS keeper_map_strict_mode = 1",
            params={"k": key, "h": holder},
        )
    except Exception as e:  # another waiter took it over first: keep waiting
        if not _lost_race(e):
            log.warning("oauth claims: Keeper refused a takeover of %s: %s", key, e)
            raise Busy(key) from e


@asynccontextmanager
async def holding(ch: Any, key: str) -> AsyncIterator[None]:
    """Run the block as the only transition on `key`. Raises Busy when
    another one holds it for longer than a short wait, or Keeper fails."""
    if not await _available(ch):
        yield
        return
    holder = secrets.token_hex(8)
    deadline = asyncio.get_running_loop().time() + _HOLD_S
    with statement_timeout(_STATEMENT_S):
        try:
            await _wait_for(ch, key, holder)
            token = _deadline.set(deadline)
            try:
                yield
            finally:
                _deadline.reset(token)
        finally:
            await _release(ch, key, holder)  # also a takeover that then failed


async def _wait_for(ch: Any, key: str, holder: str) -> None:
    for wait in (*_WAITS_S, None):
        if await _claim(ch, key, holder):
            return
        if wait is None:
            raise Busy(key)
        await _take_over_if_stale(ch, key, holder)
        await asyncio.sleep(wait)


async def _release(ch: Any, key: str, holder: str) -> None:
    try:
        await ch.execute(
            f"ALTER TABLE {_TABLE} DELETE "
            "WHERE claim_key = {k:String} AND holder = {h:String} "
            "SETTINGS keeper_map_strict_mode = 1",
            params={"k": key, "h": holder},
        )
    except Exception as e:  # noqa: BLE001 — a key left behind is taken over once stale
        if not _lost_race(e):
            log.warning("oauth claims: could not drop %s: %s", key, e)
