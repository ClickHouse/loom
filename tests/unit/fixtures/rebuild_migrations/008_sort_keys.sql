-- Sort keys that match the reads.
--
-- code_test_facets and code_pr_reviews lead with path, the column their
-- heaviest reads look up (one file's facets, the review comments on a set of
-- files). code_pr_history and code_pr_reviews lose their monthly partitions,
-- which split a small table into over a hundred tiny parts that every read
-- opens. Every statement restates the table's columns unchanged.
--
-- code_test_facets keeps its key columns, reordered, so its merges keep the
-- same rows. code_pr_reviews gains path in its key: a review comment's path is
-- fixed on the forge and every writer copies it from there (the fetched-marker
-- row, comment_id 0, always has path ''), so one comment is still one row.
-- Dropping a partition key only lets rows of one key merge across months.
--
-- A sort key is CREATE-only, so these tables are rebuilt, not altered. On a
-- database that already holds rows, run scripts/ops/table_rebuild.py for
-- each table before deploying this file; the runner then finds the target
-- layout and records the file. See server/table_rebuild.py.
--
-- loom: rebuild-if-empty code_test_facets
-- loom: rebuild-if-empty code_pr_reviews
-- loom: rebuild-if-empty code_pr_history

CREATE TABLE loom.code_test_facets
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `facet_kind` LowCardinality(String),
    `name` String,
    `path` String,
    `ordinal` UInt16 DEFAULT 0,
    `line` UInt32 DEFAULT 0,
    `confidence` Float32 DEFAULT 1,
    `indexed_commit` FixedString(40) DEFAULT '',
    `tombstoned_at` Nullable(DateTime) DEFAULT NULL,
    `created_at` DateTime DEFAULT now(),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9))),
    INDEX idx_code_test_facets_name name TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_code_test_facets_kind facet_kind TYPE set(0) GRANULARITY 1
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, path, facet_kind, name, ordinal)
SETTINGS index_granularity = 1024;

CREATE TABLE loom.code_pr_reviews
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `pr_number` UInt32,
    `comment_id` UInt64,
    `path` String DEFAULT '',
    `line` UInt32 DEFAULT 0,
    `author` String DEFAULT '',
    `is_bot` Bool DEFAULT false,
    `body` String DEFAULT '',
    `in_reply_to` UInt64 DEFAULT 0,
    `comment_created_at` DateTime,
    `tombstoned_at` Nullable(DateTime) DEFAULT NULL,
    `created_at` DateTime DEFAULT now(),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)))
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, path, pr_number, comment_id)
SETTINGS index_granularity = 1024;

CREATE TABLE loom.code_pr_history
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `pr_number` UInt32,
    `title` String DEFAULT '',
    `body_excerpt` String DEFAULT '',
    `author` String DEFAULT '',
    `labels` Array(LowCardinality(String)) DEFAULT [],
    `files` Array(String) DEFAULT [],
    `assignees` Array(String) DEFAULT [],
    `additions` UInt32 DEFAULT 0,
    `deletions` UInt32 DEFAULT 0,
    `merge_commit_sha` FixedString(40) DEFAULT '',
    `merged_at` DateTime,
    `opened_at` DateTime DEFAULT toDateTime(0),
    `tombstoned_at` Nullable(DateTime) DEFAULT NULL,
    `created_at` DateTime DEFAULT now(),
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9))),
    `lexemes` String DEFAULT '' CODEC(ZSTD(3)),
    `linked_issues` Array(UInt32) DEFAULT [],
    `target_branch` LowCardinality(String) DEFAULT '',
    INDEX idx_code_pr_files files TYPE bloom_filter(0.01) GRANULARITY 4,
    INDEX idx_code_pr_merged merged_at TYPE minmax GRANULARITY 8,
    INDEX idx_code_pr_linked linked_issues TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, pr_number)
SETTINGS index_granularity = 8192;
