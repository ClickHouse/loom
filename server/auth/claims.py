"""One sign-in state transition at a time per grant.

A transition (spending a code, refresh token or device code; signing in on a
device grant; approving one) reads the grant's row in loom.oauth_state, checks
it and writes the next version. Two requests doing that at once could both
pass the check, so each transition holds the grant's key in loom.oauth_claims
(KeeperMap, schemas/migrations/029_oauth_claims.sql) for its read and write:
Keeper refuses a second insert of a key under keeper_map_strict_mode, so of
two concurrent transitions exactly one runs and the other waits or is told to
retry. The key is dropped when the transition is done; the written row keeps
the state from then on. A key older than `_STALE_S` belongs to a holder that
died and is taken over.

A ClickHouse without a Keeper has no claims table: transitions then run
unclaimed, as they did before, and /v1/health says so
(`oauth_transitions_not_atomic`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from server.core.schema.migrate_lease import is_keeper_table, key_exists

log = logging.getLogger(__name__)

_TABLE = "loom.oauth_claims"
_TABLE_SQL = "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'oauth_claims'"
_STALE_S = 10  # far longer than any transition's read and write
_WAITS_S = (0.05, 0.1, 0.2, 0.4)  # then the request is told to retry

# Whether loom.oauth_claims exists here: None until the boot probe or the
# first transition asks.
_atomic: bool | None = None


class Busy(Exception):
    """Another request is moving this grant right now."""


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


async def _insert(ch: Any, key: str) -> bool:
    try:
        await ch.execute(
            f"INSERT INTO {_TABLE} (claim_key, claimed_at) "
            "SETTINGS keeper_map_strict_mode = 1 VALUES ({k:String}, now64(3))",
            params={"k": key},
        )
    except Exception as e:  # a taken key is the answer; anything else is re-raised
        if key_exists(e):
            return False
        raise
    return True


async def _take_over_if_stale(ch: Any, key: str) -> None:
    await ch.execute(
        f"ALTER TABLE {_TABLE} DELETE WHERE claim_key = {{k:String}} "
        f"AND claimed_at < now64(3) - INTERVAL {_STALE_S} SECOND",
        params={"k": key},
    )


@asynccontextmanager
async def holding(ch: Any, key: str) -> AsyncIterator[None]:
    """Run the block as the only transition on `key`. Raises Busy when
    another one holds it for longer than a short wait."""
    if not await _available(ch):
        yield
        return
    for wait in (*_WAITS_S, None):
        if await _insert(ch, key):
            break
        if wait is None:
            raise Busy(key)
        await _take_over_if_stale(ch, key)
        await asyncio.sleep(wait)
    try:
        yield
    finally:
        try:
            await ch.execute(
                f"ALTER TABLE {_TABLE} DELETE WHERE claim_key = {{k:String}}", params={"k": key}
            )
        except Exception as e:  # noqa: BLE001 — a key left behind is taken over once stale
            log.warning("oauth claims: could not drop %s: %s", key, e)
