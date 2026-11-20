-- The nonce `loom cell prepare` writes to prove a cell's probe listener reads these stores
-- (docs/architecture/cells.md, "Preparing a cell"). One row per probe, deleted once the
-- echo is checked; it holds no memory and no org.
CREATE TABLE IF NOT EXISTS loom.cell_probe (
    name text COLLATE "C" PRIMARY KEY,
    nonce text NOT NULL,
    written_at timestamptz NOT NULL DEFAULT now()
);
