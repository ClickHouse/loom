-- Which chunks a knowledge head serves: the revision and version of the last commit whose
-- write inserted the source's chunks (an add, an upsert, a bulk add or an ingest's
-- publish). A delete, a metadata edit or a restore keeps them; a first head written by
-- one of those leaves them NULL. Search checks a chunk against them: the rule is in
-- docs/architecture/storage-authority.md ("Knowledge search").
ALTER TABLE loom.knowledge_heads ADD COLUMN IF NOT EXISTS chunks_revision integer;
ALTER TABLE loom.knowledge_heads ADD COLUMN IF NOT EXISTS chunks_version bigint;
