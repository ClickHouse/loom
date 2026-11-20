-- Where the router forwards a cell's requests: an address inside the deployment's
-- network (`http://loom.<namespace>.svc:7777`), never the cell's public host, which
-- names the router itself. `loom cell attach` writes it; '' until then.
ALTER TABLE loom_router.cells ADD COLUMN IF NOT EXISTS upstream_url text NOT NULL DEFAULT '';
