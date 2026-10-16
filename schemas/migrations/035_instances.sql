-- One heartbeat row per running Loom process (server/core/instances.py): its
-- build and schema version, the fingerprints of what must match across the
-- deployment, whether it serves traffic, the workers lease it contends for and
-- its start time, renewed every LOOM_INSTANCE_HEARTBEAT_SECONDS. `heartbeat_at`
-- is the server's clock. The live rows give the rate limiter its replica count
-- (serving rows only), the workers lease the newer builds contending for it and
-- the leader the configurations still in service.
--
-- A Keeper key per process: a beat is one Keeper write and no part.
CREATE TABLE IF NOT EXISTS loom.instances
(
    `instance_id` String,
    `deployment` UInt8 DEFAULT 0,
    `label` String DEFAULT '',
    `host` String DEFAULT '',
    `pid` UInt32 DEFAULT 0,
    `build_commit` String DEFAULT '',
    `schema_version` UInt32 DEFAULT 0,
    `serving` UInt8 DEFAULT 1,
    `workers_lease` String DEFAULT '',
    `settings_fp` String DEFAULT '',
    `keys_fp` String DEFAULT '',
    `config_fp` String DEFAULT '',
    `started_at` DateTime64(3) DEFAULT now64(3),
    `heartbeat_at` DateTime64(3) DEFAULT now64(3),
    `stopped` UInt8 DEFAULT 0
)
ENGINE = KeeperMap('/loom/instances')
PRIMARY KEY instance_id;
