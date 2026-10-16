-- Sign-in state that changes once, kept in ClickHouse Keeper.
--
-- One key per credential a sign-in spends or moves (a code, a device grant,
-- a single-use mark per identity provider callback, an MCP approval awaiting
-- its click) and per session (which refresh token is its current one). server/auth/claims.py changes a value
-- only by a strict-mode compare-and-set, so of two requests spending one
-- credential exactly one wins. Each key carries its credential's expiry and is
-- swept soon after it; the key limit caps what a flood of unauthenticated
-- sign-in starts can hold in Keeper (past it, sign-in answers 503).
--
-- Tolerated on a server without a Keeper (or without keeper_map_path_prefix):
-- the statement is skipped, sign-in answers 503, and /v1/health reports
-- `oauth_signin_needs_keeper`.
-- loom: tolerate-error 999
-- loom: tolerate-error 139
-- loom: tolerate-error 36
CREATE TABLE IF NOT EXISTS loom.oauth_signin_state
(
    `claim_key` String,
    `value` String,
    `expires_at` UInt32
)
ENGINE = KeeperMap('/loom/oauth_signin_state', 500000)
PRIMARY KEY claim_key;
