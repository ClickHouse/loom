-- The OAuth exchange in flight on each code or refresh token, kept in ClickHouse Keeper.
--
-- /oauth/token inserts the credential's key under keeper_map_strict_mode = 1
-- before it reads the credential, and deletes it when the exchange is decided:
-- Keeper refuses a second INSERT of the same key, so of two concurrent
-- exchanges of one credential exactly one runs, and one that starts after the
-- release finds the credential consumed. A row lives for one exchange; one a
-- crashed exchange left is taken over once stale. `claim_key` is
-- `<record_type>:<credential id>` (the sha256 the store keys it by).
--
-- Tolerated on a server without a Keeper or without keeper_map_path_prefix
-- (OSS without deploy/clickhouse-config/keeper.xml): the statement is
-- skipped, the file records as applied, and the exchange runs unclaimed, as
-- /v1/health then says.
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
