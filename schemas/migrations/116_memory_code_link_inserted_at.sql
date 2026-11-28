-- ClickHouse stamps each link row as it is inserted. The recheck's catch-up and the
-- guidance read it to find links written after a pass read the table.
ALTER TABLE loom.memory_code_links ADD COLUMN IF NOT EXISTS inserted_at DateTime64(3) DEFAULT now64(3);
-- rows stored before the column exists would read as inserted now: give them this time
ALTER TABLE loom.memory_code_links MATERIALIZE COLUMN inserted_at;
