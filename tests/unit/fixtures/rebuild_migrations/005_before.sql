-- The three tables 008_sort_keys.sql rebuilds, as the baseline before it created them.

-- loom.code_pr_history
CREATE TABLE IF NOT EXISTS loom.code_pr_history
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
PARTITION BY toYYYYMM(merged_at)
ORDER BY (org, namespace, pr_number)
SETTINGS index_granularity = 8192;

-- loom.code_pr_reviews
CREATE TABLE IF NOT EXISTS loom.code_pr_reviews
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
    `version` UInt64 DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9))),
    INDEX idx_code_pr_reviews_path path TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(comment_created_at)
ORDER BY (org, namespace, pr_number, comment_id)
SETTINGS index_granularity = 8192;

-- loom.code_test_facets
CREATE TABLE IF NOT EXISTS loom.code_test_facets
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
    INDEX idx_code_test_facets_path path TYPE bloom_filter(0.01) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace, facet_kind, name, path, ordinal)
SETTINGS index_granularity = 8192;
