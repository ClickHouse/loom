-- A cell's router credential during a rotation: `loom cell credential --next` records the
-- new credential's SHA-256 here, the router accepts it beside `credential_sha256`, and
-- `--finish` makes it current (`--revert` drops it). A hash names one cell, current or
-- next: the unique indexes hold each column, and the operator API checks across both
-- under the directory lock.
ALTER TABLE loom_router.cells ADD COLUMN IF NOT EXISTS credential_next_sha256 text
    CHECK (credential_next_sha256 IS NULL OR credential_next_sha256 ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX IF NOT EXISTS cells_next_credential
    ON loom_router.cells (credential_next_sha256) WHERE credential_next_sha256 IS NOT NULL;
