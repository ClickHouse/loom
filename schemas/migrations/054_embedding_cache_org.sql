-- The org a cached vector was embedded for (server/embedding/_cache.py), so an
-- org's rows can be found and deleted without the texts they were keyed by:
-- the key already hashes the org, and the text is never stored. '' on rows a
-- build before it wrote; their keys hash no org, so this build's reads never
-- find them, and the 30-day TTL takes them.
ALTER TABLE loom.embedding_cache
    ADD COLUMN IF NOT EXISTS `org` LowCardinality(String) DEFAULT '';
