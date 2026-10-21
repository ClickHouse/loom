-- A resolved projection failure leaves 30 days after it was resolved: point writes'
-- retirement intents are resolved by the thousand. An active one never expires, nor an
-- abandoned one, left for an operator. A resolve always stamps resolved_at; the coalesce
-- only gives the TTL the non-Nullable type it requires. Merges apply the TTL: no part is
-- rewritten when the migration runs, which may be during a rolling upgrade. See
-- docs/architecture/memory-architecture.md, "Replay".
ALTER TABLE loom.memory_projection_failures
    MODIFY TTL coalesce(resolved_at, created_at) + INTERVAL 30 DAY DELETE WHERE status = 'resolved'
    SETTINGS materialize_ttl_after_modify = 0;
