"""One exchange at a time per OAuth code or refresh token, claimed in Keeper.

The OAuth store marks a credential used with a new ReplacingMergeTree
version, and a read-then-mark cannot keep two concurrent exchanges of one
credential apart. So /oauth/token claims the credential's key in
`loom.oauth_claims` under keeper_map_strict_mode before it reads it, and
releases the claim once the exchange is decided: Keeper refuses the second
INSERT, so a concurrent presenter is told to retry (it may be the owner
retrying), and its retry, after the release, finds the credential consumed.
A claim a crashed exchange left is taken over once older than `STALE_S`,
with Keeper's compare-and-swap, so of two takers one wins.

Without a Keeper the migration skipped the table, the exchange runs
unclaimed (two presenters racing can both succeed) and /v1/health says so.
"""

from __future__ import annotations

import enum
import logging
import uuid
from typing import Any

from server.core.ch import statement_timeout
from server.core.schema.migrate_lease import (
    OAUTH_CLAIMS_TABLE_SQL,
    is_keeper_table,
    key_exists,
    lost_race,
)

log = logging.getLogger(__name__)

# A claim older than this was left by an exchange that died. Above the bound
# /oauth/token puts on one exchange (`EXCHANGE_TIMEOUT_S`), so a live one is
# never taken over.
STALE_S = 120
EXCHANGE_TIMEOUT_S = 60

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
_RELEASE = (
    "ALTER TABLE loom.oauth_claims DELETE "
    f"WHERE claim_key = {{k:String}} AND holder = {{me:String}} {_STRICT}"
)

# Whether loom.oauth_claims exists here: None until the boot probe or the
# first exchange asks.
_atomic: bool | None = None


class Claim(enum.Enum):
    HELD = "held"  # this exchange holds the key
    TAKEN = "taken"  # another exchange of the credential is running
    UNCLAIMED = "unclaimed"  # no claims table here


def claims_atomic() -> bool | None:
    """Whether OAuth exchanges are claimed in Keeper here; None = not asked yet."""
    return _atomic


async def probe(ch: Any) -> None:
    """At boot, so /v1/health names `oauth_claims_not_atomic` before any exchange."""
    try:
        await _available(ch)
    except Exception as e:  # noqa: BLE001 — a failed probe leaves it to the first exchange
        log.warning("oauth claims: could not read system.tables: %s", e)


async def claim(ch: Any, key: str) -> tuple[Claim, str]:
    """Claim `key` for one exchange: (outcome, holder id to release with).
    Raises when Keeper cannot answer, so the caller refuses retryably rather
    than run an exchange it could not keep apart."""
    if not await _available(ch):
        return Claim.UNCLAIMED, ""
    me = uuid.uuid4().hex
    params = {"k": key, "me": me, "stale": STALE_S}
    try:
        await ch.execute(_INSERT, params=params)
        return Claim.HELD, me
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
    return (Claim.HELD, me) if await _holder(ch, key) == me else (Claim.TAKEN, "")


async def release(ch: Any, key: str, holder: str) -> None:
    """Drop this exchange's claim; one taken over since is the taker's and
    stays. A release that fails or stalls leaves the claim to go stale."""
    try:
        with statement_timeout(5):
            await ch.execute(_RELEASE, params={"k": key, "me": holder})
    except Exception as e:  # noqa: BLE001 — the exchange is decided; a kept claim goes stale
        log.warning("oauth claims: release of %s failed: %s", key, e)


async def _holder(ch: Any, key: str) -> str:
    row = await ch.query_one(_HOLDER, params={"k": key})
    return str(row["holder"]) if row else ""


async def _available(ch: Any) -> bool:
    global _atomic
    if _atomic is None:
        _atomic = is_keeper_table(await ch.query_one(OAUTH_CLAIMS_TABLE_SQL))
        if not _atomic:
            log.info("oauth: no loom.oauth_claims (no Keeper); exchanges are not claimed")
    return _atomic
