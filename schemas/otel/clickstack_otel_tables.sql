-- ClickStack OTel tables for a deployment without a HyperDX install that
-- already created them (LOOM_OTEL_EXPORTER=ch). Apply once, by hand:
--
--     clickhouse-client --multiquery < schemas/otel/clickstack_otel_tables.sql
--
-- The tables live in the `default` database, where HyperDX's "Add Source"
-- looks and where server/observability/otel.py inserts; they are not
-- migrations and loom.* never references them. Single-node DDL (no ON
-- CLUSTER); a replicated database replicates it. Every statement is
-- IF NOT EXISTS, so a rerun changes nothing, and an existing table keeps its
-- own indexes.
--
-- Columns and indexes follow the ClickStack schema:
-- https://clickhouse.com/docs/use-cases/observability/clickstack/ingesting-data/schemas
-- Keep them identical, or a ClickStack collector pointed at the same database
-- fails to insert. The metrics tables' mapValues(Attributes) bloom filter is
-- also what the erasure cascade's ClickStack leg reads by (`otel_erasure_index`
-- in `loom doctor`).

CREATE TABLE IF NOT EXISTS default.otel_traces (
    Timestamp        DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TraceId          String CODEC(ZSTD(1)),
    SpanId           String CODEC(ZSTD(1)),
    ParentSpanId     String CODEC(ZSTD(1)),
    TraceState       String CODEC(ZSTD(1)),
    SpanName         LowCardinality(String) CODEC(ZSTD(1)),
    SpanKind         LowCardinality(String) CODEC(ZSTD(1)),
    ServiceName      LowCardinality(String) CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeName        String CODEC(ZSTD(1)),
    ScopeVersion     String CODEC(ZSTD(1)),
    SpanAttributes   Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    Duration         Int64 CODEC(ZSTD(1)),
    StatusCode       LowCardinality(String) CODEC(ZSTD(1)),
    StatusMessage    String CODEC(ZSTD(1)),
    `Events.Timestamp`  Array(DateTime64(9)) CODEC(ZSTD(1)),
    `Events.Name`       Array(LowCardinality(String)) CODEC(ZSTD(1)),
    `Events.Attributes` Array(Map(LowCardinality(String), String)) CODEC(ZSTD(1)),
    `Links.TraceId`     Array(String) CODEC(ZSTD(1)),
    `Links.SpanId`      Array(String) CODEC(ZSTD(1)),
    `Links.TraceState`  Array(String) CODEC(ZSTD(1)),
    `Links.Attributes`  Array(Map(LowCardinality(String), String)) CODEC(ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_res_attr_key   mapKeys(ResourceAttributes)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_attr_key   mapKeys(SpanAttributes)      TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_attr_value mapValues(SpanAttributes)    TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_duration Duration TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Timestamp)
ORDER BY (ServiceName, SpanName, toUnixTimestamp(Timestamp), TraceId)
TTL toDateTime(Timestamp) + INTERVAL 180 DAY
SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192;

-- ---- Metrics ----
-- One table per OTel metric type. Loom writes sums, histograms and gauges;
-- the exponential-histogram and summary tables stay empty but exist because
-- HyperDX's metrics source expects the full set and the erasure cascade
-- deletes from each.

CREATE TABLE IF NOT EXISTS default.otel_metrics_sum (
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl  String CODEC(ZSTD(1)),
    ScopeName          String CODEC(ZSTD(1)),
    ScopeVersion       String CODEC(ZSTD(1)),
    ScopeAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl     String CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String) CODEC(ZSTD(1)),
    MetricName         String CODEC(ZSTD(1)),
    MetricDescription  String CODEC(ZSTD(1)),
    MetricUnit         String CODEC(ZSTD(1)),
    Attributes         Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix      DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimeUnix           DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    Value              Float64 CODEC(ZSTD(1)),
    Flags              UInt32 CODEC(ZSTD(1)),
    Exemplars Nested(
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix DateTime64(9),
        Value Float64,
        SpanId String,
        TraceId String
    ) CODEC(ZSTD(1)),
    AggregationTemporality Int32 CODEC(ZSTD(1)),
    IsMonotonic Bool CODEC(ZSTD(1)),
    INDEX idx_res_attr_key     mapKeys(ResourceAttributes)  TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value   mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_key   mapKeys(ScopeAttributes)     TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_value mapValues(ScopeAttributes)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_key         mapKeys(Attributes)          TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_value       mapValues(Attributes)        TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, TimeUnix)
TTL toDateTime(TimeUnix) + INTERVAL 90 DAY
SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192;

CREATE TABLE IF NOT EXISTS default.otel_metrics_histogram (
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl  String CODEC(ZSTD(1)),
    ScopeName          String CODEC(ZSTD(1)),
    ScopeVersion       String CODEC(ZSTD(1)),
    ScopeAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl     String CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String) CODEC(ZSTD(1)),
    MetricName         String CODEC(ZSTD(1)),
    MetricDescription  String CODEC(ZSTD(1)),
    MetricUnit         String CODEC(ZSTD(1)),
    Attributes         Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix      DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimeUnix           DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    Count              UInt64 CODEC(ZSTD(1)),
    Sum                Float64 CODEC(ZSTD(1)),
    BucketCounts       Array(UInt64) CODEC(ZSTD(1)),
    ExplicitBounds     Array(Float64) CODEC(ZSTD(1)),
    Exemplars Nested(
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix DateTime64(9),
        Value Float64,
        SpanId String,
        TraceId String
    ) CODEC(ZSTD(1)),
    Flags UInt32 CODEC(ZSTD(1)),
    Min Float64 CODEC(ZSTD(1)),
    Max Float64 CODEC(ZSTD(1)),
    AggregationTemporality Int32 CODEC(ZSTD(1)),
    INDEX idx_res_attr_key     mapKeys(ResourceAttributes)  TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value   mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_key   mapKeys(ScopeAttributes)     TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_value mapValues(ScopeAttributes)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_key         mapKeys(Attributes)          TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_value       mapValues(Attributes)        TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, TimeUnix)
TTL toDateTime(TimeUnix) + INTERVAL 90 DAY
SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192;

CREATE TABLE IF NOT EXISTS default.otel_metrics_gauge (
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl  String CODEC(ZSTD(1)),
    ScopeName          String CODEC(ZSTD(1)),
    ScopeVersion       String CODEC(ZSTD(1)),
    ScopeAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl     String CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String) CODEC(ZSTD(1)),
    MetricName         String CODEC(ZSTD(1)),
    MetricDescription  String CODEC(ZSTD(1)),
    MetricUnit         String CODEC(ZSTD(1)),
    Attributes         Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix      DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimeUnix           DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    Value              Float64 CODEC(ZSTD(1)),
    Flags              UInt32 CODEC(ZSTD(1)),
    Exemplars Nested(
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix DateTime64(9),
        Value Float64,
        SpanId String,
        TraceId String
    ) CODEC(ZSTD(1)),
    INDEX idx_res_attr_key     mapKeys(ResourceAttributes)  TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value   mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_key   mapKeys(ScopeAttributes)     TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_value mapValues(ScopeAttributes)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_key         mapKeys(Attributes)          TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_value       mapValues(Attributes)        TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, TimeUnix)
TTL toDateTime(TimeUnix) + INTERVAL 90 DAY
SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192;

CREATE TABLE IF NOT EXISTS default.otel_metrics_exponential_histogram (
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl  String CODEC(ZSTD(1)),
    ScopeName          String CODEC(ZSTD(1)),
    ScopeVersion       String CODEC(ZSTD(1)),
    ScopeAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl     String CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String) CODEC(ZSTD(1)),
    MetricName         String CODEC(ZSTD(1)),
    MetricDescription  String CODEC(ZSTD(1)),
    MetricUnit         String CODEC(ZSTD(1)),
    Attributes         Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix      DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimeUnix           DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    Count              UInt64 CODEC(Delta(8), ZSTD(1)),
    Sum                Float64 CODEC(ZSTD(1)),
    Scale              Int32 CODEC(ZSTD(1)),
    ZeroCount          UInt64 CODEC(ZSTD(1)),
    PositiveOffset     Int32 CODEC(ZSTD(1)),
    PositiveBucketCounts Array(UInt64) CODEC(ZSTD(1)),
    NegativeOffset     Int32 CODEC(ZSTD(1)),
    NegativeBucketCounts Array(UInt64) CODEC(ZSTD(1)),
    Exemplars Nested(
        FilteredAttributes Map(LowCardinality(String), String),
        TimeUnix DateTime64(9),
        Value Float64,
        SpanId String,
        TraceId String
    ) CODEC(ZSTD(1)),
    Flags UInt32 CODEC(ZSTD(1)),
    Min Float64 CODEC(ZSTD(1)),
    Max Float64 CODEC(ZSTD(1)),
    AggregationTemporality Int32 CODEC(ZSTD(1)),
    INDEX idx_res_attr_key     mapKeys(ResourceAttributes)  TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value   mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_key   mapKeys(ScopeAttributes)     TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_value mapValues(ScopeAttributes)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_key         mapKeys(Attributes)          TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_value       mapValues(Attributes)        TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, TimeUnix)
TTL toDateTime(TimeUnix) + INTERVAL 90 DAY
SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192;

CREATE TABLE IF NOT EXISTS default.otel_metrics_summary (
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ResourceSchemaUrl  String CODEC(ZSTD(1)),
    ScopeName          String CODEC(ZSTD(1)),
    ScopeVersion       String CODEC(ZSTD(1)),
    ScopeAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeDroppedAttrCount UInt32 CODEC(ZSTD(1)),
    ScopeSchemaUrl     String CODEC(ZSTD(1)),
    ServiceName        LowCardinality(String) CODEC(ZSTD(1)),
    MetricName         String CODEC(ZSTD(1)),
    MetricDescription  String CODEC(ZSTD(1)),
    MetricUnit         String CODEC(ZSTD(1)),
    Attributes         Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    StartTimeUnix      DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimeUnix           DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    Count              UInt64 CODEC(Delta(8), ZSTD(1)),
    Sum                Float64 CODEC(ZSTD(1)),
    ValueAtQuantiles Nested(
        Quantile Float64,
        Value Float64
    ) CODEC(ZSTD(1)),
    Flags UInt32 CODEC(ZSTD(1)),
    INDEX idx_res_attr_key     mapKeys(ResourceAttributes)  TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value   mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_key   mapKeys(ScopeAttributes)     TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_scope_attr_value mapValues(ScopeAttributes)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_key         mapKeys(Attributes)          TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_attr_value       mapValues(Attributes)        TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(TimeUnix)
ORDER BY (ServiceName, MetricName, Attributes, TimeUnix)
TTL toDateTime(TimeUnix) + INTERVAL 90 DAY
SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192;

CREATE TABLE IF NOT EXISTS default.otel_logs (
    Timestamp        DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimestampTime    DateTime DEFAULT toDateTime(Timestamp),
    TraceId          String CODEC(ZSTD(1)),
    SpanId           String CODEC(ZSTD(1)),
    TraceFlags       UInt8,
    SeverityText     LowCardinality(String) CODEC(ZSTD(1)),
    SeverityNumber   UInt8,
    ServiceName      LowCardinality(String) CODEC(ZSTD(1)),
    Body             String CODEC(ZSTD(1)),
    ResourceSchemaUrl String CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeSchemaUrl   String CODEC(ZSTD(1)),
    ScopeName        String CODEC(ZSTD(1)),
    ScopeVersion     String CODEC(ZSTD(1)),
    ScopeAttributes  Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    LogAttributes    Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1,
    INDEX idx_res_attr_key   mapKeys(ResourceAttributes)   TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_log_attr_key   mapKeys(LogAttributes)        TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_log_attr_value mapValues(LogAttributes)      TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_body Body TYPE tokenbf_v1(32768, 3, 0) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(TimestampTime)
ORDER BY (ServiceName, SeverityText, TimestampTime, Timestamp)
TTL TimestampTime + INTERVAL 180 DAY
SETTINGS ttl_only_drop_parts = 1, index_granularity = 8192;
