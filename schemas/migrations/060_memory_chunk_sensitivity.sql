-- The sensitivity label of a verbatim chunk (server/memory/domain/sensitivity.py,
-- `passage_sensitivity`): 'high' when its text reads as a credential, else ''. A
-- 'high' chunk is stored as written with no embedding, so no search leg returns it,
-- and `memory.get` serves it by key with an audit row. '' on chunks a build before
-- it wrote; every read that serves chunk text checks those with `reads_high`.
ALTER TABLE loom.memory_chunks
    ADD COLUMN IF NOT EXISTS `sensitivity` LowCardinality(String) DEFAULT '';
