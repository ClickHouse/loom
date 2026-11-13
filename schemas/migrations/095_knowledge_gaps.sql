-- Knowledge gaps (docs/architecture/knowledge-gaps.md).
--
-- loom.search_misses: one row per search the caller found nothing in (a memory.search
-- that abstained or answered at high retrieval risk, a knowledge.search with no close
-- hit, a loom_search where memory and every other section missed), written when
-- LOOM_KNOWLEDGE_GAPS_ENABLED is on. `asker` is the person the token stands for: its end
-- user, else its creator, else its subject. A trace that expires after 16 days. A pass
-- reads the last 7 and a build lives 7 from its write, so a miss outlives every build that
-- counted it, with two days to spare, and a user erasure finds them. Erased per user
-- (asker or user_id) and per namespace.
CREATE TABLE IF NOT EXISTS loom.search_misses
(
    `ts` DateTime DEFAULT now(),
    -- The recording's own id: it deletes its row when an erasure fenced it.
    `miss_id` String,
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    -- The identity tuple. A miss is the namespace's, so agent and session stay '-'.
    `agent` LowCardinality(String) DEFAULT '-',
    `user_id` String DEFAULT '-',
    `session_id` String DEFAULT '-',
    `asker` String,
    `normalized` String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (org, namespace, ts)
TTL ts + INTERVAL 16 DAY
SETTINGS non_replicated_deduplication_window = 1000;

-- loom.knowledge_gaps: the misses of a namespace grouped by the daily knowledge_gaps
-- worker. Each pass writes a new `build` of the namespace; readers take its latest
-- build (each writes one marker row, empty gap_id), and older builds expire. A query
-- fewer than knowledge_gaps_min_askers distinct askers asked is never written. It keeps
-- counts, never who asked: a user erasure deletes the builds of the namespaces the user
-- has misses in, which the next pass rebuilds without the user's misses.
CREATE TABLE IF NOT EXISTS loom.knowledge_gaps
(
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `build` UInt64,
    `gap_id` String,
    `head` String,
    `samples` Array(String),
    `askers` UInt32,
    `asks` UInt32,
    `last_at` DateTime,
    `built_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (org, namespace, build, gap_id)
TTL built_at + INTERVAL 7 DAY
SETTINGS non_replicated_deduplication_window = 1000;
