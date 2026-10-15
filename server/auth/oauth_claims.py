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

from server.core.keeper_claims import Claim, KeeperClaims

__all__ = ["CLAIMS", "Claim", "claims_atomic"]

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
