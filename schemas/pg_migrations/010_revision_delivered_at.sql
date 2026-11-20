-- When ClickHouse came to hold a revision, for the payload clearing pass
-- (`server/memory/workers/pg_payload_clear.py`). A completion stamps `delivered_at` only
-- on a revision it delivers more than 15 minutes after its commit
-- (`pg_memory.LATE_DELIVERY_S`), so an on-time write never touches the journal again. A
-- delivered revision with no stamp was delivered within 15 minutes of `committed_at`,
-- except one an older build delivered late
-- (docs/architecture/rolling-upgrades.md, rule 43).
--
-- The index holds the revisions whose payload the pass may clear: a profile revision
-- keeps its payload, since the redaction's reach into the profiles matches on it. Its
-- predicate is `pg_memory.CLEARABLE`, word for word, so the pass's read can use it. It
-- is built in this file's transaction, which a large table forbids
-- (docs/development/migrations.md, "Postgres migrations"): the first release that writes
-- this journal carries this file, so it is small wherever this file runs.
ALTER TABLE loom.memory_revisions ADD COLUMN IF NOT EXISTS delivered_at timestamptz;

CREATE INDEX IF NOT EXISTS memory_revisions_payload
    ON loom.memory_revisions ((coalesce(delivered_at, committed_at)), org, namespace, seq)
    WHERE row IS NOT NULL AND NOT (row->>'agent' = '-' AND row->>'memory_type' = 'profile');
