-- When each triple row arrived, to the microsecond. Two values of a single-valued
-- relation order by the day they start, then by this arrival, so every writer, an
-- Undo and a replay of either agree on which one is current, even within one second.
-- `last_seen` keeps its whole second. A row stored before this column reads its
-- `last_seen` second (computed on read, so no part is rewritten).
ALTER TABLE loom.entity_triples
    ADD COLUMN IF NOT EXISTS seen_at DateTime64(6) DEFAULT toDateTime64(last_seen, 6);
