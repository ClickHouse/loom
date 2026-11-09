-- A revision's metadata, kept beside its payload (`row`) in typed columns, so a reader
-- of the journal needs the payload only for the revision's content. A trigger fills
-- them from `row` whenever a writer sets it, whatever build it is, and leaves them when
-- `row` is cleared; each falls back to the loom.memory column's default when `row`
-- leaves the field out. A revision written before this file has them NULL, and a reader
-- takes the field from its `row` then (`pg_memory._meta`): nothing here rewrites the
-- journal, so the file holds the table's lock only for its catalog changes. `row` may
-- be NULL: how each reader tolerates that is in docs/architecture/storage-authority.md,
-- "Revision metadata".
ALTER TABLE loom.memory_revisions
    ADD COLUMN IF NOT EXISTS agent text COLLATE "C",
    ADD COLUMN IF NOT EXISTS session_id text COLLATE "C",
    ADD COLUMN IF NOT EXISTS memory_key text COLLATE "C",
    ADD COLUMN IF NOT EXISTS memory_type text,
    ADD COLUMN IF NOT EXISTS status text,
    ADD COLUMN IF NOT EXISTS source_trace text,
    ADD COLUMN IF NOT EXISTS tombstoned boolean,
    ADD COLUMN IF NOT EXISTS redacted_at text,
    ADD COLUMN IF NOT EXISTS redaction_reason text,
    ALTER COLUMN row DROP NOT NULL;

CREATE OR REPLACE FUNCTION loom.revision_metadata() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.row IS NOT NULL THEN
        NEW.agent := coalesce(NEW.row->>'agent', '');
        NEW.session_id := coalesce(NEW.row->>'session_id', '');
        NEW.memory_key := coalesce(NEW.row->>'memory_key', '');
        NEW.memory_type := coalesce(NEW.row->>'memory_type', '');
        NEW.status := coalesce(NEW.row->>'status', 'active');
        NEW.source_trace := coalesce(NEW.row->>'source_trace', '');
        NEW.tombstoned := NEW.row->>'tombstoned_at' IS NOT NULL;
        NEW.redacted_at := NEW.row->>'redacted_at';
        NEW.redaction_reason := coalesce(NEW.row->>'redaction_reason', '');
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS memory_revisions_metadata ON loom.memory_revisions;
CREATE TRIGGER memory_revisions_metadata
    BEFORE INSERT OR UPDATE OF row ON loom.memory_revisions
    FOR EACH ROW EXECUTE FUNCTION loom.revision_metadata();
