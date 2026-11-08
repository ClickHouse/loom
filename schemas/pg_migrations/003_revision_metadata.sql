-- A revision's metadata, kept beside its payload (`row`) in typed columns, so a reader
-- of the journal needs the payload only for the revision's content. A trigger fills
-- them from `row` whenever a writer sets it, whatever build it is, and leaves them when
-- `row` is cleared; each falls back to the loom.memory column's default when `row`
-- leaves the field out. `row` may be NULL: the readers of this build read a revision's
-- content from ClickHouse's `memory_versions` then, checked against these columns.
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

-- The journal written before this file: the trigger fills each row as it is rewritten.
UPDATE loom.memory_revisions SET row = row WHERE agent IS NULL;

ALTER TABLE loom.memory_revisions
    ALTER COLUMN agent SET NOT NULL,
    ALTER COLUMN session_id SET NOT NULL,
    ALTER COLUMN memory_key SET NOT NULL,
    ALTER COLUMN memory_type SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN source_trace SET NOT NULL,
    ALTER COLUMN tombstoned SET NOT NULL,
    ALTER COLUMN redaction_reason SET NOT NULL;

-- A user's profile versions, which a redaction of a memory they were folded from scrubs:
-- on the typed columns, which hold whether or not the payload does.
CREATE INDEX IF NOT EXISTS memory_revisions_profile_meta
    ON loom.memory_revisions (org, namespace, user_id)
    WHERE agent = '-' AND memory_type = 'profile';

-- The revisions that still hold their payload, oldest first: what clearing payloads
-- once their delivery has settled reads.
CREATE INDEX IF NOT EXISTS memory_revisions_payload
    ON loom.memory_revisions (committed_at) WHERE row IS NOT NULL;
