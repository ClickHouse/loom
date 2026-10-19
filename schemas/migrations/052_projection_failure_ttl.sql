-- A resolved projection failure leaves 30 days after it was resolved: point writes'
-- retirement intents are resolved by the thousand. An active one never expires, nor an
-- abandoned one, left for an operator. A resolve always stamps resolved_at; the coalesce
-- only gives the TTL the non-Nullable type it requires. See
-- docs/architecture/memory-architecture.md, "Replay".
ALTER TABLE loom.memory_projection_failures
    MODIFY TTL coalesce(resolved_at, created_at) + INTERVAL 30 DAY DELETE WHERE status = 'resolved';
