-- The graph projections a `memory.set` leaves running, and each namespace's last
-- graph write: a graph walk on any Loom process waits (bounded) for the projections
-- of its namespace that began before it, so a writer reads its own links, and a
-- remembered walk is retired by a later write. Every stamp is the server's clock.
--
-- Keeper keys: a projection holds one of its namespace's slot keys, and one more
-- key holds the namespace's last write, so a walk reads its namespace by primary
-- key and no write makes a part.
--
-- `namespace_write_keys` is the same idea for every plane: a namespace's last write
-- as one Keeper key, overwritten by each write (server/core/namespace_write_marks.py),
-- so a read on any replica knows the caller just wrote there.
CREATE TABLE IF NOT EXISTS loom.graph_projections
(
    `token` String,
    `org` String,
    `namespace` String,
    `started_at` DateTime64(3) DEFAULT now64(3),
    `finished` UInt8 DEFAULT 0
)
ENGINE = KeeperMap('/loom/graph_projections')
PRIMARY KEY token;

CREATE TABLE IF NOT EXISTS loom.namespace_write_keys
(
    `key` String,
    `last_write` DateTime64(3) DEFAULT now64(3)
)
ENGINE = KeeperMap('/loom/namespace_write_keys')
PRIMARY KEY key;
