-- Sign-in state that changes once, kept in ClickHouse Keeper.
--
-- One key per credential a sign-in spends or moves (a code, an identity
-- provider round trip in flight, a device grant) and per session (which
-- refresh token is its current one). server/auth/claims.py changes a value
-- only by a strict-mode compare-and-set, so of two requests spending one
-- credential exactly one wins. Keys are swept a month after their last change.
--
-- Tolerated on a server without a Keeper (or without keeper_map_path_prefix):
-- the statement is skipped, sign-in answers 503, and /v1/health reports
-- `oauth_signin_needs_keeper`.
-- loom: tolerate-error 999
-- loom: tolerate-error 139
-- loom: tolerate-error 36
CREATE TABLE IF NOT EXISTS loom.oauth_claims
(
    `claim_key` String,
    `value` String,
    `updated_at` DateTime64(3)
)
ENGINE = KeeperMap('/loom/oauth_claims')
PRIMARY KEY claim_key;
