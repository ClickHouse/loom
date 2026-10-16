"""ClickHouse connection, query shaping and vector storage."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from server.settings._env import JsonObject


class ClickHouseSettings(BaseModel):
    # ch_secure=None → wrapper infers TLS from port (8443 → True).
    ch_host: str = "localhost"
    # ClickHouse HTTP port; 8443 (the Cloud HTTPS port) turns TLS on when `ch_secure` is unset.
    ch_port: int = 8123
    # User name the server connects to ClickHouse as.
    ch_user: str = "loom"
    # Password for `ch_user`; the default only suits a local development server.
    ch_password: str = "loomdev"
    # Database that holds every Loom table and that migrations run against.
    ch_database: str = "loom"
    # The cluster `clusterAllReplicas` reads to find a stopped Loom process's
    # queries on every node (`default` on ClickHouse Cloud). A server without
    # it answers for itself; '' reads only the connected server.
    ch_cluster: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]*$")] = "default"
    # Connect over HTTPS. Unset infers TLS from the port (8443 on, anything else off).
    ch_secure: bool | None = None
    # Append `sql:<shape12> <first 200 chars>` of each statement to its
    # `log_comment` ("<op> <request_id>"), so query_log rows group by shape.
    # Off by default: readers that split log_comment on spaces gain tokens.
    ch_log_comment_statement_digest: bool = False
    # HTTP connection pool size; the driver default (~8) exhausts under bulk
    # writes plus workers. Size to expected request concurrency.
    ch_pool_maxsize: int = 64
    # Seconds an idle pooled connection is kept. Must stay BELOW the Cloud load
    # balancer's idle cutoff (8 s) so loom closes first: a peer-closed pooled
    # TLS connection is never reaped under uvloop (`LOOM_UVICORN_LOOP=uvloop`).
    ch_keepalive_timeout: float = 5.0
    # A point lookup that MISSES right after a write (get after set, verify,
    # update_by_id, delete) re-reads once with select_sequential_consistency=1.
    # Costs only on a miss; a no-op on single-node MergeTree.
    ch_read_your_writes: bool = True
    # memory.search on a namespace this process wrote within this many seconds
    # reads with select_sequential_consistency from the start. 0 = off (the
    # empty-pool retry remains the backstop).
    ch_read_your_writes_window_s: float = 20.0
    # Per-class statement deadline, seconds, applied as `max_execution_time`
    # and as a client-side bound that frees the pool slot. 0 = no bound for
    # that class.
    ch_timeout_read_s: float = 30.0
    # Deadline, seconds, for INSERT and UPDATE statements; 0 = no bound.
    ch_timeout_write_s: float = 60.0
    # Deadline, seconds, for DDL statements (CREATE, ALTER, DROP); 0 = no bound.
    ch_timeout_ddl_s: float = 300.0
    # Max wait, ms, for a pool slot before failing fast with 503 OVERLOADED.
    # 0 = no slot gate.
    ch_pool_wait_ms: int = 500
    # Ceilings for the reads a caller shapes (code.grep, code.search with a
    # pattern or globs, memory.aggregate, memory.record.query). A ceiling only
    # lowers the statement's own value, so no request can widen it; 0 = none.
    # The shipped loom_reader profile (nlp_user_settings.xml) caps these three at
    # their defaults, with a floor no statement can go under (time: 0.05 s,
    # code.grep's statement floor), so none can be set to 0 even in the SQL.
    # Raising a setting above its cap needs the profile raised too; a 0 sends
    # no ceiling, so the profile's cap applies (code.grep's deadline is then
    # clamped to ch_timeout_read_s, which the profile's cap matches).
    # Seconds one such read may run (the read class default).
    ch_caller_shaped_max_execution_s: float = 30.0
    # Bytes of memory one such read may use.
    ch_caller_shaped_max_memory_bytes: int = 4_000_000_000
    # Rows one such read may scan; past it the read fails (422), never a partial answer.
    ch_caller_shaped_max_rows_to_read: int = 500_000_000
    # ClickHouse user the caller-shaped reads run as, with `ch_password`; the
    # shipped config defines `loom_reader` (SELECT only). Empty = `ch_user`.
    ch_reader_user: str = ""

    # JSON map {traffic class: ClickHouse settings} applied by
    # `server.core.ch.statement_settings` to every read (an explicit per-call
    # setting wins). `priority`: 1 served first, larger waits, 0 opts out;
    # `max_threads` caps CPU per statement. Neither changes the rows returned.
    # `background` (workers, operator CLIs) bypasses the filesystem cache on
    # a miss so a backfill scan does not evict interactive data.
    ch_settings_by_traffic_class: Annotated[str, JsonObject] = (
        '{"production": {"priority": 1}, "interactive": {"priority": 2}, '
        '"bench": {"priority": 10, "max_threads": 4}, '
        '"background": {"priority": 5, "max_threads": 3, '
        '"read_from_filesystem_cache_if_exists_otherwise_bypass_cache": 1}}'
    )
    # Apply pending migrations at boot. Set False for any server started from
    # a branch against a shared service, or it applies unmerged migrations.
    boot_migrate: bool = True
    # Seconds after which a holder that stopped renewing is replaced.
    boot_migrate_lease_ttl_s: int = 300
    # Seconds a contender waits for the holder before boot fails (exit 1, the
    # container restarts); also how long a booting process waits for the
    # provider configuration lock (a sibling's rebuild step may hold it).
    boot_migrate_lease_wait_s: float = 900.0
    # Seconds a migration file waits for the mutations it started (MATERIALIZE,
    # UPDATE, a column type change) to finish on every replica; past it the
    # file fails unrecorded.
    migrate_mutation_wait_s: int = 600

    # HNSW candidate list size (`ef_search`) for the helper legs, emitted via
    # vector_search_settings(); tracks ClickHouse's default. The
    # main leg scales its own value with top_k.
    vector_search_ef_search: int = 256
    # Post-filter over-fetch for a `vector_similarity` (HNSW) index: the index
    # returns top-N before the tenant predicate drops rows. Sent on uncoded
    # vector reads; coded reads use `vector_search_quantized_fetch_multiplier`.
    vector_search_index_fetch_multiplier: float = 10.0
    # Send `vector_search_use_quantized_codes = 1` on vector-ranked reads whose
    # column has a `Quantized(...)` codec and whose table has no patch parts:
    # a codes-first scan plus full-precision rescore. A replica that rejects
    # the setting (code 115) drops the fragment and retries.
    vector_search_quantized_codes: bool = True
    # Over-fetch for the codes-first read: stage 1 keeps LIMIT × this by code
    # distance, stage 2 rescores. Only read when the codes setting is sent.
    vector_search_quantized_fetch_multiplier: float = 3.0
    # Seconds between re-reads of which vector columns are coded and which
    # coded tables carry patch parts. A write that embeds reads again on its own
    # when the reading is older than a few seconds (codes_state.FRESH_READING_S).
    vector_codes_state_probe_seconds: int = 60
