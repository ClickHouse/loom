"""One exchange at a time per OAuth code or refresh token, claimed in Keeper.

The OAuth store marks a credential used with a new ReplacingMergeTree
version, and a read-then-mark cannot keep two concurrent exchanges of one
credential apart. So /oauth/token claims the credential's key in
`loom.oauth_claims` under keeper_map_strict_mode before it reads it, and
releases the claim once the exchange is decided: Keeper refuses the second
INSERT, so a concurrent presenter is refused, and one that comes after the
release finds the credential consumed. A claim a crashed exchange left is
taken over once older than `STALE_S`.

Without a Keeper the migration skipped the table, the exchange runs
unclaimed (two presenters racing can both succeed) and /v1/health says so.
"""

from __future__ import annotations

import enum
import logging
import uuid
from typing import Any

from server.core.schema.migrate_lease import is_keeper_table, key_exists

log = logging.getLogger(__name__)

# An exchange takes a few ClickHouse round trips; a claim older than this was
# left by one that died.
STALE_S = 120

_TABLE_SQL = "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'oauth_claims'"
_STRICT = "SETTINGS keeper_map_strict_mode = 1"
_INSERT = (
    "INSERT INTO loom.oauth_claims (claim_key, holder, claimed_at) "
    f"{_STRICT}, async_insert = 0 "
    "VALUES ({k:String}, {me:String}, now64(3))"
)
_DROP_STALE = (
    "ALTER TABLE loom.oauth_claims DELETE WHERE claim_key = {k:String} "
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
    for _ in range(2):
        try:
            await ch.execute(_INSERT, params={"k": key, "me": me})
            return Claim.HELD, me
        except Exception as e:
            if not key_exists(e):
                raise
        await ch.execute(_DROP_STALE, params={"k": key, "stale": STALE_S})
    return Claim.TAKEN, ""


async def release(ch: Any, key: str, holder: str) -> None:
    """Drop this exchange's claim; a claim that outlived `STALE_S` and was
    taken over is the taker's and stays. A release that fails leaves the claim
    to go stale."""
    try:
        await ch.execute(_RELEASE, params={"k": key, "me": holder})
    except Exception as e:  # noqa: BLE001 — the exchange is decided; a kept claim goes stale
        log.warning("oauth claims: release of %s failed: %s", key, e)


async def _available(ch: Any) -> bool:
    global _atomic
    if _atomic is None:
        _atomic = is_keeper_table(await ch.query_one(_TABLE_SQL))
        if not _atomic:
            log.info("oauth: no loom.oauth_claims (no Keeper); exchanges are not claimed")
    return _atomic
