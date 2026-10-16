-- The content hash of the write an embedding row mirrors, carried through its
-- metadata re-emits: the deferred conflict judge stamps, and the conflict sweep
-- re-judges, exactly that write. '' on rows written before it.
ALTER TABLE loom.memory_embeddings
    ADD COLUMN IF NOT EXISTS `content_sha256` String DEFAULT '';
