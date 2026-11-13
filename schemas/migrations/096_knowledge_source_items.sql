-- loom.knowledge_sources: each source's items, and the kind and credential of a source
-- a build without items cannot read. A source that fits the id-list config is
-- written as before, with `items_json` beside it; any other is written with `kind`
-- empty, its kind in `source_kind`, its credential in `secret_enc` (purpose
-- `knowledge_sources.secret`), `credential_enc` empty and `config_json` holding no
-- item. A row written before these columns reads them empty and its items from
-- `config_json`.
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS source_kind LowCardinality(String) DEFAULT '';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS items_json String DEFAULT '[]';
ALTER TABLE loom.knowledge_sources ADD COLUMN IF NOT EXISTS secret_enc String DEFAULT '';
