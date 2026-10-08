-- The task inbox lease table lives in ClickHouse Keeper.
--
-- A claim is a conditional ALTER UPDATE under keeper_map_strict_mode = 1:
-- Keeper checks the znode version in the same multi-op as the write, so two
-- claimers cannot both win. Only live leases are kept.
--
-- Tolerated on a server without a Keeper or without keeper_map_path_prefix
-- (OSS without deploy/clickhouse-config/keeper.xml): the statement is
-- skipped, the file records as applied, and the repository detects the
-- missing table at boot and arbitrates claims through loom.task_claims.
-- loom: tolerate-error 999
-- loom: tolerate-error 139
-- loom: tolerate-error 36
CREATE TABLE IF NOT EXISTS loom.task_leases
(
    `task_id` String,
    `org` String,
    `inbox` String,
    `owner` String,
    `lease_until` DateTime,
    `fence` UInt32,
    `status` String,
    `updated_at` DateTime
)
ENGINE = KeeperMap('/loom/task_leases')
PRIMARY KEY task_id;
