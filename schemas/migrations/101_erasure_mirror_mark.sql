-- A receipt that counted a mirror stays private even if a restore loses the mirror
-- registry. Builds that do not read the mark cannot serve mirrored sources.
ALTER TABLE loom.erasure_requests ADD COLUMN IF NOT EXISTS mirror_marked Bool DEFAULT false;
