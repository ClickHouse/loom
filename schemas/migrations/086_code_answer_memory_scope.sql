-- The memory a cached code.ask answer quoted: the bound memory namespace and the keys
-- of the excerpts its synthesis was shown, '' and [] for an answer drawn from the code
-- index alone. An answer carries memory only from a namespace of its own org, so the
-- erasure saga finds it by (org, memory_namespace) for a namespace scope and by
-- memory_keys for a user scope.
ALTER TABLE loom.code_answers
    ADD COLUMN IF NOT EXISTS `memory_namespace` LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS `memory_keys` Array(String) DEFAULT [];

-- code.ask reads whether a quoted key has a redacted version by key, below the namespace.
ALTER TABLE loom.memory_versions
    ADD INDEX IF NOT EXISTS memory_key_bloom_idx memory_key TYPE bloom_filter(0.01) GRANULARITY 4;
