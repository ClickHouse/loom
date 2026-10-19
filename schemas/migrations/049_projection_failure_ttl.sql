-- A projection failure that is no longer active (resolved, or abandoned for an operator)
-- leaves 30 days after it was resolved, or after it was recorded when no resolve time was
-- stamped: point writes' retirement intents are resolved by the thousand, and an active
-- one never expires. See docs/architecture/memory-architecture.md, "Replay".
ALTER TABLE loom.memory_projection_failures
    MODIFY TTL coalesce(resolved_at, created_at) + INTERVAL 30 DAY DELETE WHERE status != 'active';
