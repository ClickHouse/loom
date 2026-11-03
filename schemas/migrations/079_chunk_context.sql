-- A knowledge chunk's context: the document title, description and section path the
-- writer put before the chunk text when embedding it. The lexical leg scores it with
-- the text, so a section whose subject is named only in its document's title or
-- headings matches on that name too. Rows written before this column read ''.

-- Kept out of merges like content_text, set before the index exists so no merge builds
-- it: a skip index a merge cannot build fails every merge of the table
-- (docs/architecture/clickhouse.md, "Merges and skip indexes").
ALTER TABLE loom.chunks MODIFY SETTING exclude_materialize_skip_indexes_on_merge = 'content_text,context_text';

ALTER TABLE loom.chunks ADD COLUMN IF NOT EXISTS `context` String DEFAULT '' AFTER `content`;

ALTER TABLE loom.chunks ADD COLUMN IF NOT EXISTS `context_lc` String MATERIALIZED lowerUTF8(context) AFTER `context`;

ALTER TABLE loom.chunks ADD INDEX IF NOT EXISTS context_text context_lc TYPE text(tokenizer = 'splitByNonAlpha') GRANULARITY 100000000;

-- Document frequency counts a chunk once per term whether the term is in its text, its
-- context or both: this view and chunk_term_freq_mv write the same chunk key, which
-- uniqExact counts once.
CREATE MATERIALIZED VIEW IF NOT EXISTS loom.chunk_context_term_freq_mv TO loom.chunk_term_freq
AS SELECT
    org,
    namespace,
    term,
    uniqExactState((source_uri, chunk_idx)) AS df_state,
    now() AS df_added_at
FROM loom.chunks
ARRAY JOIN arrayDistinct(arrayFilter(t -> (length(t) >= 3), arrayMap(t -> lowerUTF8(t), splitByNonAlpha(context)))) AS term
WHERE (content != '') AND (context != '') AND (doc_superseded_at IS NULL)
GROUP BY
    org,
    namespace,
    term;
