-- loom.erasure_requests.census: the residue census of a saga's last complete count, every
-- substrate it counted with its count, zeros included, as a JSON object. `residue` keeps
-- only the non-zero counts; this column is what `erasure.status` returns as the receipt.
-- Empty until a census that counted every store ran. Counts only, no content.
ALTER TABLE loom.erasure_requests ADD COLUMN IF NOT EXISTS census String DEFAULT '';
