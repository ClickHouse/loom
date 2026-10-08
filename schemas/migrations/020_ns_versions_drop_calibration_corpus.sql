-- Every ReplacingMergeTree version is nanoseconds. These tables still
-- defaulted to milliseconds; their writers now stamp nanoseconds, and a row
-- written without a version must not lose to them under FINAL. A nanosecond
-- value is larger than every millisecond one already stored, so the rows a
-- writer adds from now on keep winning.
ALTER TABLE loom.categories
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

ALTER TABLE loom.revoked_tokens
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

ALTER TABLE loom.erasure_reversals
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

ALTER TABLE loom.memory_schemas
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

ALTER TABLE loom.memory_schema_fields
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

ALTER TABLE loom.memory_records
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

ALTER TABLE loom.memory_record_fields
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

ALTER TABLE loom.memory_record_conflicts
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

-- The records' version mirror keeps the records' column definitions.
ALTER TABLE loom.memory_record_versions
    MODIFY COLUMN `version` DEFAULT toUInt64(toUnixTimestamp64Nano(now64(9)));

-- Nothing writes loom.namespace_calibration_corpus; per-namespace thresholds
-- come from code.calibrate into loom.namespace_calibration_result.
DROP TABLE IF EXISTS loom.namespace_calibration_corpus;
