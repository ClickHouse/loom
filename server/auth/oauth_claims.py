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

from typing import Any

from server.core.keeper_claims import Claim, KeeperClaims, shielded
from server.core.schema.migrate_lease import is_keeper_table

__all__ = ["Claim", "claim", "claims_atomic", "is_keeper_table", "probe", "release"]

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

CLAIMS = KeeperClaims(
    "oauth_claims",
    stale_s=STALE_S,
    claim_timeout_s=CLAIM_TIMEOUT_S,
    release_timeout_s=RELEASE_TIMEOUT_S,
)


def claims_atomic() -> bool | None:
    """Whether OAuth exchanges are claimed in Keeper here; None = not asked yet."""
    return CLAIMS.atomic


async def probe(ch: Any) -> None:
    """At boot, so /v1/health names `oauth_claims_not_atomic` before any exchange,
    and to sweep the stale claims: one an exchange kept on purpose (its consume
    did not finish) or failed to release is otherwise removed only when its
    credential is presented again."""
    await CLAIMS.probe(ch)


async def claim(ch: Any, key: str) -> tuple[Claim, str]:
    """Claim `key` for one exchange: (outcome, holder id to release with)."""
    return await CLAIMS.claim(ch, key)


async def release(ch: Any, key: str, holder: str) -> None:
    await CLAIMS.release(ch, key, holder)


async def release_shielded(ch: Any, key: str, holder: str) -> None:
    await shielded(release(ch, key, holder), name="oauth_claims.release")
