-- What each running process reports about its cell (server/core/instances.py), beside
-- its loom.instances row and under the same key, renewed with it: the LOOM_CELL it
-- runs with ('' when it is not a cell), the tenancy mode it last read, and its
-- configuration fingerprint (server/core/cell.py), whose fields `loom cell prepare`
-- and attach compare across the cell's processes. A table of its own because a
-- KeeperMap table takes no new column. A build older than this file writes no row:
-- prepare reads a live loom.instances row without one as a build before cells.
--
-- A Keeper key per process: a beat is one Keeper write and no part.
CREATE TABLE IF NOT EXISTS loom.cell_heartbeats
(
    `instance_id` String,
    `cell` String DEFAULT '',
    `tenancy_mode` String DEFAULT '',
    `fingerprint` String DEFAULT '',
    `heartbeat_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = KeeperMap('/loom/cell_heartbeats')
PRIMARY KEY instance_id;
