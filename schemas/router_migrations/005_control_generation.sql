-- A cell control claim invalidates requests from every earlier local lock owner.
-- Fresh values on insertion and each claim also fence removal/re-registration.
ALTER TABLE loom_router.cells ADD COLUMN mutation_generation uuid NOT NULL DEFAULT gen_random_uuid();
