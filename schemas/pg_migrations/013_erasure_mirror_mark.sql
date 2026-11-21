-- Keep the privacy mark when this ledger restores a ClickHouse erasure request.
ALTER TABLE loom.erasure_ledger ADD COLUMN IF NOT EXISTS mirror_marked boolean NOT NULL DEFAULT false;
