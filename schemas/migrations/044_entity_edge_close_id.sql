-- The id of the close that ended an edge row (empty when none did); the reopen after a
-- racing write reads it. See docs/architecture/memory-architecture.md, "Rows that hold facts".
ALTER TABLE loom.entity_edges
    ADD COLUMN IF NOT EXISTS closed_by String DEFAULT '';
