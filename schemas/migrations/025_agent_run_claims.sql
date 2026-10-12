-- Who began each run id, kept in ClickHouse Keeper.
--
-- run.begin inserts the key under keeper_map_strict_mode = 1 before it writes
-- the agent_runs row: Keeper refuses a second INSERT of the same key, so of two
-- concurrent begins on one (org, namespace, run_id) exactly one claims it and
-- the other starts under a fresh run id. KeeperMap takes a one-column primary
-- key, so `claim_key` is the JSON list [org, namespace, run_id].
--
-- Tolerated on a server without a Keeper or without keeper_map_path_prefix
-- (OSS without deploy/clickhouse-config/keeper.xml): the statement is
-- skipped, the file records as applied, and run.begin finds no table at boot
-- and keeps its read-then-insert check.
-- loom: tolerate-error 999
-- loom: tolerate-error 139
-- loom: tolerate-error 36
CREATE TABLE IF NOT EXISTS loom.agent_run_claims
(
    `claim_key` String,
    `org` String,
    `namespace` String,
    `run_id` String,
    `user_id` String,
    `session_id` String,
    `claimed_at` DateTime64(3)
)
ENGINE = KeeperMap('/loom/agent_run_claims')
PRIMARY KEY claim_key;
