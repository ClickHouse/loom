-- loom.documents: a mirrored document's permission list
-- (docs/architecture/document-sources.md, The permission list), as canonical JSON written
-- by the turn of the source that owns it; empty on every other row, and on a mirrored row
-- it reads as `unread`, which admits nobody. A retirement's tombstone carries none, an
-- erasure purges a mirror's rows, and no read op returns it.
ALTER TABLE loom.documents ADD COLUMN IF NOT EXISTS access_json String DEFAULT '';
