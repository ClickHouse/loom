-- loom:online-index loom_router.placements_by_cell_org ON loom_router.placements (cell, org)
CREATE INDEX CONCURRENTLY placements_by_cell_org ON loom_router.placements
USING btree (cell COLLATE "C", org COLLATE "C");
