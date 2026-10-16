-- The Undo that wrote a `row-restore` row. A replay of that Undo finds its own rows by
-- this id and adds none, while another Undo, even a microsecond apart, never takes
-- them for its own. Empty on every other row.
ALTER TABLE loom.entity_triples
    ADD COLUMN IF NOT EXISTS restore_id String DEFAULT '';

ALTER TABLE loom.entity_edges
    ADD COLUMN IF NOT EXISTS restore_id String DEFAULT '';
