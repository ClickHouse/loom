-- The router's directory (docs/architecture/cells.md, "The router"): which cells
-- exist, which org each is placed in, and which host names which cell. It lives in
-- the router's own Postgres database, apart from every cell's stores; no cell reads
-- it directly. It holds org names and cell metadata, never memory content, so no
-- erasure step runs here: a placement outlives its org's erasure, and the name never
-- comes free for another cell.

CREATE SCHEMA IF NOT EXISTS loom_router;

-- One row per cell. `loom cell attach` writes it; the placement API reads it.
CREATE TABLE IF NOT EXISTS loom_router.cells (
    cell text COLLATE "C" PRIMARY KEY CHECK (cell ~ '^[a-z0-9-]{1,32}$'),
    -- The first cell keeps the deployment's shared host; at most one row is first.
    is_first boolean NOT NULL DEFAULT false,
    -- Its own public host, as `normalise_host` writes it.
    host text COLLATE "C" NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('attaching', 'serving')),
    attach_started_at timestamptz,
    -- The highest tenancy mode attach has set in the cell, and when ('' = none yet).
    mode text NOT NULL DEFAULT '' CHECK (mode IN ('', 'recording', 'enforced')),
    mode_set_at timestamptz,
    -- Closed: its placement calls answer 503. NULL quota = no limit.
    closed boolean NOT NULL DEFAULT false,
    placements_max bigint CHECK (placements_max IS NULL OR placements_max >= 0),
    -- The orgs placed in it, counted under its row lock as each is placed.
    placed bigint NOT NULL DEFAULT 0 CHECK (placed >= 0),
    -- SHA-256 (hex) of the credential the cell calls the router with: one cell's alone,
    -- so the credential names its cell.
    credential_sha256 text NOT NULL UNIQUE CHECK (credential_sha256 ~ '^[0-9a-f]{64}$'),
    -- The stores: the deployment id both of them record, and the document bucket.
    deployment_id text COLLATE "C" NOT NULL UNIQUE,
    bucket text NOT NULL DEFAULT '',
    bucket_prefix text NOT NULL DEFAULT '',
    -- The bearer-token issuers it alone accepts, its root-secret fingerprint, a later
    -- cell's probe URL, and its break-glass bots' names and key thumbprints.
    issuers text[] NOT NULL DEFAULT '{}',
    keys_fp text NOT NULL DEFAULT '',
    probe_url text NOT NULL DEFAULT '',
    break_glass jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(break_glass) = 'array'),
    -- The configuration fingerprints it accepts: `current` and, during an update, `next`.
    current_fp jsonb CHECK (current_fp IS NULL OR jsonb_typeof(current_fp) = 'object'),
    next_fp jsonb CHECK (next_fp IS NULL OR jsonb_typeof(next_fp) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS cells_one_first ON loom_router.cells ((true)) WHERE is_first;

-- Which cell holds each org: one row per org, written once and never changed.
CREATE TABLE IF NOT EXISTS loom_router.placements (
    org text COLLATE "C" PRIMARY KEY CHECK (org NOT IN ('', '-', '*')),
    cell text COLLATE "C" NOT NULL REFERENCES loom_router.cells (cell),
    placed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS placements_by_cell ON loom_router.placements (cell);

CREATE OR REPLACE FUNCTION loom_router.placements_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'a placement is written once and never changed or deleted';
END;
$$;
DROP TRIGGER IF EXISTS placements_immutable ON loom_router.placements;
CREATE TRIGGER placements_immutable BEFORE UPDATE OR DELETE ON loom_router.placements
    FOR EACH ROW EXECUTE FUNCTION loom_router.placements_immutable();
DROP TRIGGER IF EXISTS placements_no_truncate ON loom_router.placements;
CREATE TRIGGER placements_no_truncate BEFORE TRUNCATE ON loom_router.placements
    FOR EACH STATEMENT EXECUTE FUNCTION loom_router.placements_immutable();

-- Every cell's host and the shared host's aliases, as `normalise_host` writes them: a
-- host names one cell, so no later cell can take the shared host or another's.
CREATE TABLE IF NOT EXISTS loom_router.hosts (
    host text COLLATE "C" PRIMARY KEY,
    cell text COLLATE "C" NOT NULL REFERENCES loom_router.cells (cell)
);

-- What changed the directory, by whom: placements and every operator change.
CREATE TABLE IF NOT EXISTS loom_router.audit (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,
    action text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object')
);
