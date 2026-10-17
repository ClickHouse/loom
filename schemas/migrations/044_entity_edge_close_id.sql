-- The close that ended an edge row: a fresh id per `end_edges` call, stamped with the
-- end. A row naming the pair that lands while the close runs opens again only the rows
-- this close ended, found by this id, and the reopen clears it. Two observations alike
-- in every other column stay apart. Empty on a row no close of this build ended.
ALTER TABLE loom.entity_edges
    ADD COLUMN IF NOT EXISTS closed_by String DEFAULT '';
