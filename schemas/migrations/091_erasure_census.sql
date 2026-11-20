-- loom.erasure_requests.census: the residue census on this checkpoint, every substrate it
-- counted with its count, zeros included, as a JSON object. `residue` keeps only the
-- non-zero counts; the latest row's census is what `erasure.status` returns as the receipt.
-- Empty on a row whose run has not counted every store. Counts only, no content.
ALTER TABLE loom.erasure_requests ADD COLUMN IF NOT EXISTS census String DEFAULT '';
