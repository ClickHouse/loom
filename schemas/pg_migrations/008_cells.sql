-- A cell's own state (docs/architecture/cells.md). `loom cell prepare` writes
-- loom.cell_first, loom.tenancy_mode and loom.cell_self together; a process with
-- LOOM_CELL set serves only while those three are read and agree with its
-- configuration. Nothing older than this file reads or writes any of them.

-- Whether this cell is the deployment's first, and its name: one row, written once.
CREATE TABLE IF NOT EXISTS loom.cell_first (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    is_first boolean NOT NULL,
    cell text COLLATE "C" NOT NULL,
    written_at timestamptz NOT NULL DEFAULT now()
);

-- Which cell is first never changes once written.
CREATE OR REPLACE FUNCTION loom.cell_first_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'loom.cell_first is written once and never changed';
END;
$$;
DROP TRIGGER IF EXISTS cell_first_immutable ON loom.cell_first;
CREATE TRIGGER cell_first_immutable BEFORE UPDATE OR DELETE ON loom.cell_first
    FOR EACH ROW EXECUTE FUNCTION loom.cell_first_immutable();
DROP TRIGGER IF EXISTS cell_first_no_truncate ON loom.cell_first;
CREATE TRIGGER cell_first_no_truncate BEFORE TRUNCATE ON loom.cell_first
    FOR EACH STATEMENT EXECUTE FUNCTION loom.cell_first_immutable();
-- ALWAYS: they fire under session_replication_role = replica too.
ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_immutable;
ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_no_truncate;

-- The cell's tenancy mode: `recording` (a verified request records a new org while the
-- directory answers that the cell is `attaching`) or `enforced` (the cell serves
-- exactly the orgs it holds a row for). One row.
CREATE TABLE IF NOT EXISTS loom.tenancy_mode (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    mode text NOT NULL CHECK (mode IN ('recording', 'enforced')),
    since timestamptz NOT NULL DEFAULT now()
);

-- The orgs this cell holds: one row per org, never deleted.
CREATE TABLE IF NOT EXISTS loom.tenancy (
    org text COLLATE "C" PRIMARY KEY,
    since timestamptz NOT NULL DEFAULT now()
);

-- The cell's copy of its directory row: the configuration fingerprints the directory
-- accepted, `current` and, during a `loom cell update`, `next`. One row.
CREATE TABLE IF NOT EXISTS loom.cell_self (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    current_fp jsonb NOT NULL CHECK (jsonb_typeof(current_fp) = 'object'),
    next_fp jsonb CHECK (next_fp IS NULL OR jsonb_typeof(next_fp) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);
