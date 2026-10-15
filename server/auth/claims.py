"""Sign-in state that changes once, kept in ClickHouse Keeper.

Whatever a sign-in spends or moves lives here, one key each, in
loom.oauth_claims (KeeperMap, schemas/migrations/029_oauth_claims.sql): a
code, an identity provider round trip in flight, a device grant, and the
current refresh token of each session. Every change is a compare-and-set
(`swap`): a strict-mode KeeperMap write that Keeper applies only while the key
still holds the value the request read. Of two requests spending one
credential exactly one wins, however slow either is and whichever ClickHouse
replica serves it; nothing here depends on time.

Callers make every value unique (a nonce in it), so reading our value back
proves our write landed. A key is swept a month after its last change
(`sweep`), past the life of anything it holds.

A ClickHouse without a Keeper has no claims table. Sign-in then answers a
retryable 503 (`Busy`) rather than spend credentials unguarded, and
/v1/health says why (`oauth_signin_needs_keeper`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from server.core.schema.migrate_lease import is_keeper_table, key_exists

log = logging.getLogger(__name__)

_TABLE = "loom.oauth_claims"
_TABLE_SQL = "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'oauth_claims'"
_KEEP_DAYS = 31  # longer than a refresh token lives (30 days)
SWEEP_INTERVAL_SECONDS = 3600

# Whether loom.oauth_claims exists here: None until the boot probe or the
# first sign-in asks.
_atomic: bool | None = None


class Busy(Exception):
    """Keeper cannot answer, or there is none: the request should retry."""


class Changed(Busy):
    """Someone else changed the key since this request read it."""


def nonce() -> str:
    return secrets.token_hex(8)


def transitions_atomic() -> bool | None:
    """Whether this ClickHouse has the claims table; None = not asked yet."""
    return _atomic


async def _available(ch: Any) -> bool:
    global _atomic
    if _atomic is None:
        _atomic = is_keeper_table(await ch.query_one(_TABLE_SQL))
        if not _atomic:
            log.warning("sign-in: no %s (no Keeper); sign-in answers 503", _TABLE)
    return _atomic


async def probe(ch: Any) -> None:
    """At boot, so /v1/health names the missing table before any sign-in."""
    try:
        await _available(ch)
    except Exception as e:  # noqa: BLE001 — a failed probe leaves it to the first sign-in
        log.warning("oauth claims: could not read system.tables: %s", e)


async def _require(ch: Any) -> None:
    if not await _available(ch):
        raise Busy("sign-in needs a ClickHouse Keeper (loom.oauth_claims)")


def _lost_race(e: BaseException) -> bool:
    """Keeper's answer to a strict-mode write on a key someone else changed."""
    return key_exists(e) or "Bad version" in str(e) or "No node" in str(e)


async def get(ch: Any, key: str) -> str | None:
    await _require(ch)
    try:
        row = await ch.query_one(
            f"SELECT value FROM {_TABLE} WHERE claim_key = {{k:String}}", params={"k": key}
        )
    except Exception as e:  # Keeper unreadable: the request retries
        raise Busy(key) from e
    return str(row["value"]) if row else None


async def swap(ch: Any, key: str, old: str | None, new: str) -> str | None:
    """Set `key` to `new` if it holds `old` (None: if it is absent), and
    return what it holds afterwards: `new` if this call's write landed."""
    await _require(ch)
    for attempt in range(3):
        try:
            if old is None:
                await ch.execute(
                    f"INSERT INTO {_TABLE} (claim_key, value, updated_at) "
                    "SETTINGS keeper_map_strict_mode = 1 VALUES ({k:String}, {v:String}, now64(3))",
                    params={"k": key, "v": new},
                )
            else:
                await ch.execute(
                    f"ALTER TABLE {_TABLE} UPDATE value = {{v:String}}, updated_at = now64(3) "
                    "WHERE claim_key = {k:String} AND value = {old:String} "
                    "SETTINGS keeper_map_strict_mode = 1",
                    params={"k": key, "v": new, "old": old},
                )
        except Exception as e:  # a lost race is an answer; Keeper failing is a retry
            if not _lost_race(e):
                log.warning("oauth claims: Keeper refused %s: %s", key, e)
                raise Busy(key) from e
        now = await get(ch, key)
        if now != old:  # ours, or the value another request wrote first
            return now
        # Still `old`, which nobody writes twice: the write has not run, or
        # this read came from a Keeper replica a moment behind it.
        await asyncio.sleep(0.05 * (attempt + 1))
    raise Busy(key)


async def sweep() -> None:
    """Drop keys untouched for a month: everything they held has expired."""
    from server.core.ch import get_client

    ch = get_client()
    if await _available(ch):
        await ch.execute(
            f"ALTER TABLE {_TABLE} DELETE WHERE updated_at < now64(3) - INTERVAL {_KEEP_DAYS} DAY"
        )
