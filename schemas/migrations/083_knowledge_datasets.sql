-- Typed snapshots of the tables an ingested file holds (a sheet, a CSV file).
--
-- loom.datasets: the catalog, one row per (source, table, document version),
-- a version row per change (publication, approval), read with FINAL. A
-- dataset is served while the document's live row has the version it was
-- published with; `snapshot_id` names the rows that serve it. `schema` is
-- JSON: each column's name, type, the type the extractor inferred and the slot
-- of loom.dataset_rows that holds it. `position` is the table's place in the
-- artifact.
CREATE TABLE IF NOT EXISTS loom.datasets
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `source_uri` String,
    `dataset_id` String,
    `doc_version` UInt64,
    `snapshot_id` UInt64,
    `revision` UInt64 DEFAULT 0,
    `table_id` String DEFAULT '',
    `position` UInt32 DEFAULT 0,
    `name` String DEFAULT '',
    `sheet` String DEFAULT '',
    `row_start` UInt32 DEFAULT 0,
    `row_end` UInt32 DEFAULT 0,
    `schema` String DEFAULT '[]',
    `schema_state` LowCardinality(String) DEFAULT 'inferred',
    `clarifications` String DEFAULT '[]',
    `row_count` UInt64 DEFAULT 0,
    `rejected_rows` UInt64 DEFAULT 0,
    `rejected` String DEFAULT '[]',
    `complete` Bool DEFAULT true,
    `artifact_sha256` String DEFAULT '',
    `approved_by` String DEFAULT '',
    `approved_at` Nullable(DateTime64(3)),
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `actor` String DEFAULT '',
    `source_class` LowCardinality(String) DEFAULT '',
    `created_at` DateTime64(3) DEFAULT now64(3),
    `updated_at` DateTime64(3) DEFAULT now64(3),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, source_uri, dataset_id, doc_version)
SETTINGS non_replicated_deduplication_window = 1000, index_granularity = 1024;

-- loom.dataset_rows: every dataset's rows in one table, a row per table row of
-- a snapshot. A column's values sit in the typed slot its schema names: text
-- and times in s1..s24, numbers and durations (seconds) in n1..n24 (the number
-- columns past those in f1..f8), dates and date-times in t1..t8, booleans in
-- b1..b8. Written once per snapshot, never re-inserted. Granules of 1,024 rows:
-- a read takes a dataset's rows by a range of the key (a sample, its first rows).
CREATE TABLE IF NOT EXISTS loom.dataset_rows
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `dataset_id` String,
    `snapshot_id` UInt64,
    `row_id` UInt32,
    `source_uri` String,
    `doc_version` UInt64,
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `actor` String DEFAULT '',
    `source_class` LowCardinality(String) DEFAULT '',
    `s1` Nullable(String),
    `s2` Nullable(String),
    `s3` Nullable(String),
    `s4` Nullable(String),
    `s5` Nullable(String),
    `s6` Nullable(String),
    `s7` Nullable(String),
    `s8` Nullable(String),
    `s9` Nullable(String),
    `s10` Nullable(String),
    `s11` Nullable(String),
    `s12` Nullable(String),
    `s13` Nullable(String),
    `s14` Nullable(String),
    `s15` Nullable(String),
    `s16` Nullable(String),
    `s17` Nullable(String),
    `s18` Nullable(String),
    `s19` Nullable(String),
    `s20` Nullable(String),
    `s21` Nullable(String),
    `s22` Nullable(String),
    `s23` Nullable(String),
    `s24` Nullable(String),
    `n1` Nullable(Decimal128(9)),
    `n2` Nullable(Decimal128(9)),
    `n3` Nullable(Decimal128(9)),
    `n4` Nullable(Decimal128(9)),
    `n5` Nullable(Decimal128(9)),
    `n6` Nullable(Decimal128(9)),
    `n7` Nullable(Decimal128(9)),
    `n8` Nullable(Decimal128(9)),
    `n9` Nullable(Decimal128(9)),
    `n10` Nullable(Decimal128(9)),
    `n11` Nullable(Decimal128(9)),
    `n12` Nullable(Decimal128(9)),
    `n13` Nullable(Decimal128(9)),
    `n14` Nullable(Decimal128(9)),
    `n15` Nullable(Decimal128(9)),
    `n16` Nullable(Decimal128(9)),
    `n17` Nullable(Decimal128(9)),
    `n18` Nullable(Decimal128(9)),
    `n19` Nullable(Decimal128(9)),
    `n20` Nullable(Decimal128(9)),
    `n21` Nullable(Decimal128(9)),
    `n22` Nullable(Decimal128(9)),
    `n23` Nullable(Decimal128(9)),
    `n24` Nullable(Decimal128(9)),
    `f1` Nullable(Float64),
    `f2` Nullable(Float64),
    `f3` Nullable(Float64),
    `f4` Nullable(Float64),
    `f5` Nullable(Float64),
    `f6` Nullable(Float64),
    `f7` Nullable(Float64),
    `f8` Nullable(Float64),
    `t1` Nullable(DateTime64(6, 'UTC')),
    `t2` Nullable(DateTime64(6, 'UTC')),
    `t3` Nullable(DateTime64(6, 'UTC')),
    `t4` Nullable(DateTime64(6, 'UTC')),
    `t5` Nullable(DateTime64(6, 'UTC')),
    `t6` Nullable(DateTime64(6, 'UTC')),
    `t7` Nullable(DateTime64(6, 'UTC')),
    `t8` Nullable(DateTime64(6, 'UTC')),
    `b1` Nullable(Bool),
    `b2` Nullable(Bool),
    `b3` Nullable(Bool),
    `b4` Nullable(Bool),
    `b5` Nullable(Bool),
    `b6` Nullable(Bool),
    `b7` Nullable(Bool),
    `b8` Nullable(Bool),
    `created_at` DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (org, namespace, dataset_id, snapshot_id, row_id)
SETTINGS non_replicated_deduplication_window = 1000, index_granularity = 1024;
