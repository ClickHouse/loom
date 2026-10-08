-- Measured coverage joins the test map. A `coverage` row records that a test
-- executed lines of a source file in an instrumented run (per-test line
-- coverage); its evidence carries the run's commit and the executed ranges.
-- Appending an enum value is a metadata change, also on a sorting-key column.
ALTER TABLE loom.code_test_map
    MODIFY COLUMN `method` Enum8('path_convention' = 1, 'include_graph' = 2, 'pr_cooccurrence' = 3, 'content_reference' = 4, 'coverage' = 5);

-- Per source file, the line ranges ANY test of that run executed. The test map
-- keeps only the lines few tests reach (a line every query runs names no
-- test), so this is what tells "executed by many tests" from "executed by
-- none": a symbol whose span meets no range here was measured unexecuted.
CREATE TABLE IF NOT EXISTS loom.code_coverage_files
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `path` String,
    `commit_sha` String DEFAULT '',
    `run_at` Nullable(DateTime) DEFAULT NULL,
    -- JSON [[start, end], ...], merged and sorted
    `lines` String DEFAULT '[]' CODEC(ZSTD(3)),
    `tests` UInt32 DEFAULT 0,
    `tombstoned_at` Nullable(DateTime) DEFAULT NULL,
    `created_at` DateTime DEFAULT now(),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, path)
SETTINGS index_granularity = 1024;
