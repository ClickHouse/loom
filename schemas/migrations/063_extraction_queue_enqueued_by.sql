-- The principal that queued a deferred `memory.set_from_messages` request, as
-- `kind:subject` (server/memory/app/extraction_queue.py), in its own column. The
-- payload carries it too, but the claim leaves out every request of an enqueuer
-- with a request held now, and reading that from each payload read every payload
-- on every pass. A row queued before this file reads '' until a hold rewrites it.
ALTER TABLE loom.memory_extraction_queue
    ADD COLUMN IF NOT EXISTS enqueued_by String DEFAULT '';
