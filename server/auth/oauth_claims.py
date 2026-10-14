"""One exchange at a time per OAuth code or refresh token, claimed in Keeper.

The OAuth store marks a credential used by writing a new ReplacingMergeTree
version, so reading it and then marking it cannot keep two concurrent
exchanges apart. /oauth/token therefore:

1. reads the credential once, so a made-up one costs no Keeper write;
2. claims its key in `loom.oauth_claims` under keeper_map_strict_mode, where
   Keeper refuses a second insert, so a concurrent presenter is told to retry
   (it may be the owner retrying);
3. reads the credential again and decides;
4. uses the credential up and releases the claim. A consume that did not
   finish may still land, so its claim is kept until it goes stale.

A claim older than `STALE_S` was left by an exchange that died, and is taken
over with Keeper's compare-and-swap, so of two takers one wins. Without a
Keeper the migration skipped the table and the exchange runs unclaimed, as
/v1/health says.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from typing import Any

from server.core.background import TaskSet
from server.core.ch import statement_timeout
from server.core.schema.migrate_lease import (
    OAUTH_CLAIMS_TABLE_SQL,
    is_keeper_table,
    key_exists,
    lost_race,
)

log = logging.getLogger(__name__)

# A claim older than STALE_S was left by an exchange that died. A live one is
# done in 80 s at most: each phase below is a wall-clock bound, retries and
# pool waits included. The rest is margin for the replicas' clocks.
STALE_S = 120
CLAIM_TIMEOUT_S = 10
# The read that finds a credential before any claim. It ends before the claim
# lands, so it adds nothing to a claim's age.
FIND_TIMEOUT_S = 10
EXCHANGE_TIMEOUT_S = 60
CONSUME_TIMEOUT_S = 10
# A release past this leaves the claim to go stale rather than hold the reply.
RELEASE_TIMEOUT_S = 10

_STRICT = "SETTINGS keeper_map_strict_mode = 1"
_INSERT = (
    f"INSERT INTO loom.oauth_claims (claim_key, holder, claimed_at) {_STRICT} "
    "VALUES ({k:String}, {me:String}, now64(3))"
)
_HOLDER = "SELECT holder FROM loom.oauth_claims WHERE claim_key = {k:String}"
# The staleness test sits in the WHERE, so it is evaluated on the znode version
# Keeper's compare-and-swap checks: of two takers one wins, the other loses the race.
_TAKE_OVER = (
    "ALTER TABLE loom.oauth_claims UPDATE holder = {me:String}, claimed_at = now64(3) "
    "WHERE claim_key = {k:String} "
    f"AND claimed_at < now64(3) - toIntervalSecond({{stale:UInt32}}) {_STRICT}"
)
_SWEEP = (
    "ALTER TABLE loom.oauth_claims DELETE "
    f"WHERE claimed_at < now64(3) - toIntervalSecond({{stale:UInt32}}) {_STRICT}"
)
_RELEASE = (
    "ALTER TABLE loom.oauth_claims DELETE "
    f"WHERE claim_key = {{k:String}} AND holder = {{me:String}} {_STRICT}"
)

# Release tasks run under a shield, kept referenced until they finish.
_releases = TaskSet()

# Whether loom.oauth_claims exists here: None until the boot probe or the
# first exchange asks. A no is asked again after `_ABSENT_RECHECK_S`.
_atomic: bool | None = None
_absent_until = 0.0
_ABSENT_RECHECK_S = 60.0


class Claim(enum.Enum):
    HELD = enum.auto()  # this exchange holds the key
    TAKEN = enum.auto()  # another exchange of the credential is running
    UNCLAIMED = enum.auto()  # no claims table here


def claims_atomic() -> bool | None:
    """Whether OAuth exchanges are claimed in Keeper here; None = not asked yet."""
    return _atomic


async def probe(ch: Any) -> None:
    """At boot, so /v1/health names `oauth_claims_not_atomic` before any exchange,
    and to sweep the stale claims: one an exchange kept on purpose (its consume
    did not finish) or failed to release is otherwise removed only when its
    credential is presented again."""
    try:
        available = await _available(ch)
    except Exception as e:  # noqa: BLE001 — a failed probe leaves it to the first exchange
        log.warning("oauth claims: could not read system.tables: %s", e)
        return
    if available:
        await _sweep(ch)


async def _sweep(ch: Any) -> None:
    """Drop every claim older than `STALE_S`. Strict mode, so a claim taken
    over meanwhile fails the sweep rather than vanish under its new holder."""
    try:
        with statement_timeout(5):
            await ch.execute(_SWEEP, params={"stale": STALE_S})
    except Exception as e:  # noqa: BLE001 — a later boot sweeps; a stale claim is taken over meanwhile
        log.warning("oauth claims: sweep of stale claims failed: %s", e)


async def claim(ch: Any, key: str) -> tuple[Claim, str]:
    """Claim `key` for one exchange: (outcome, holder id to release with).
    Raises when Keeper cannot answer, so the caller refuses retryably rather
    than run an exchange it could not keep apart."""
    me = uuid.uuid4().hex
    attempted = False
    try:
        # One wall-clock bound over the whole acquisition: a lost reply to an
        # insert that landed, then reconnects, would otherwise age the claim
        # toward `STALE_S` before the exchange even starts.
        async with asyncio.timeout(CLAIM_TIMEOUT_S):
            if not await _available(ch):
                return Claim.UNCLAIMED, ""
            attempted = True
            with statement_timeout(5):
                held = await _claim(ch, key, me)
    except BaseException:
        # An insert or takeover that answered late, or whose wait was
        # cancelled, may still have landed; a claim left behind would lock the
        # credential out until it went stale. Shielded, so a cancellation of
        # this request does not cancel the release too.
        if attempted:
            await release_shielded(ch, key, me)
        raise
    if not held:
        # Our insert may have landed with its reply lost, then read back as
        # someone else's from a lagging Keeper follower. The release matches
        # only our holder id, so it leaves a real holder's claim alone.
        await release_shielded(ch, key, me)
        return Claim.TAKEN, ""
    return Claim.HELD, me


async def _claim(ch: Any, key: str, me: str) -> bool:
    params = {"k": key, "me": me, "stale": STALE_S}
    try:
        await ch.execute(_INSERT, params=params)
        return True
    except Exception as e:
        if not key_exists(e):
            raise
    # Held: by this call when a retried insert found its own first one, else
    # by an exchange that is running, or by one that died and is taken over.
    if await _holder(ch, key) != me:
        try:
            await ch.execute(_TAKE_OVER, params=params)
        except Exception as e:
            if not lost_race(e):
                raise
    return await _holder(ch, key) == me


async def release(ch: Any, key: str, holder: str) -> None:
    """Drop this exchange's claim; one taken over since is the taker's and
    stays. A release that fails or stalls leaves the claim to go stale."""
    try:
        async with asyncio.timeout(RELEASE_TIMEOUT_S):
            with statement_timeout(5):
                await ch.execute(_RELEASE, params={"k": key, "me": holder})
    except Exception as e:  # noqa: BLE001 — the exchange is decided; a kept claim goes stale
        log.warning("oauth claims: release of %s failed: %s", key, e)


async def release_shielded(ch: Any, key: str, holder: str) -> None:
    """`release`, run to its end even when the request awaiting it is
    cancelled, again and again (anyio repeats a cancellation): the task is
    held in `_releases` and the wait for it is shielded."""
    await asyncio.shield(_releases.spawn(release(ch, key, holder), name="oauth_claims.release"))


async def _holder(ch: Any, key: str) -> str:
    row = await ch.query_one(_HOLDER, params={"k": key})
    return str(row["holder"]) if row else ""


async def _available(ch: Any) -> bool:
    """Whether loom.oauth_claims is here. A yes is kept; a no is asked again
    after `_ABSENT_RECHECK_S`, so a server that booted before the migration
    created the table starts claiming without a restart."""
    global _atomic, _absent_until
    if _atomic is True or (_atomic is False and time.monotonic() < _absent_until):
        return _atomic
    found = is_keeper_table(await ch.query_one(OAUTH_CLAIMS_TABLE_SQL))
    if not found:
        if _atomic is None:
            log.info("oauth: no loom.oauth_claims (no Keeper); exchanges are not claimed")
        _absent_until = time.monotonic() + _ABSENT_RECHECK_S
    _atomic = found
    return _atomic
