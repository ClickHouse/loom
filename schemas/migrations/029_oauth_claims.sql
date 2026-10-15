-- One sign-in state transition at a time per grant, kept in ClickHouse Keeper.
--
-- Spending a code, refresh token or device code, and each step of a device
-- grant (who signed in on it, its one-time approval), is a read then a write
-- in loom.oauth_state. The transition first inserts the grant's key here
-- under keeper_map_strict_mode = 1: Keeper refuses a second INSERT of the same
-- key, so of two concurrent transitions on one grant exactly one goes ahead.
-- The key is dropped once the state row is written, by its holder only (each
-- claim names a random holder); a key older than two minutes is a holder that
-- died and is taken over.
--
-- Tolerated on a server without a Keeper (or without keeper_map_path_prefix):
-- the statement is skipped and sign-in falls back to the read-then-write it
-- did before, which /v1/health reports as `oauth_transitions_not_atomic`.
-- loom: tolerate-error 999
-- loom: tolerate-error 139
-- loom: tolerate-error 36
CREATE TABLE IF NOT EXISTS loom.oauth_claims
(
    `claim_key` String,
    `holder` String,
    `claimed_at` DateTime64(3)
)
ENGINE = KeeperMap('/loom/oauth_claims')
PRIMARY KEY claim_key;
