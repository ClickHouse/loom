-- The grant change in flight on each (org, namespace, subject), kept in ClickHouse Keeper.
--
-- A grant op that reads the grant and then writes it claims the grant's key here first,
-- under keeper_map_strict_mode = 1, and deletes it when its write is done. Keeper refuses
-- a second INSERT of the same key, so two changes of one grant never interleave: the
-- second waits, and an approval can no longer write over a promotion or a revoke that
-- landed after its read. A claim a crashed change left is taken over once stale.
-- `claim_key` is the JSON array [org, namespace, subject].
--
-- Tolerated on a server without a Keeper or without keeper_map_path_prefix (OSS without
-- deploy/clickhouse-config/keeper.xml): the statement is skipped, the file records as
-- applied, and grant changes are kept apart within each process only, as /v1/health then
-- says.
-- loom: tolerate-error 999
-- loom: tolerate-error 139
-- loom: tolerate-error 36
CREATE TABLE IF NOT EXISTS loom.grant_claims
(
    `claim_key` String,
    `holder` String,
    `claimed_at` DateTime64(3)
)
ENGINE = KeeperMap('/loom/grant_claims')
PRIMARY KEY claim_key;
