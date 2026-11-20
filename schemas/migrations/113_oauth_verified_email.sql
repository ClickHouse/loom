-- Carry the verified sign-in address through refresh rotations.
-- Existing rows and writers that omit this field carry no email proof.
ALTER TABLE loom.oauth_state
    ADD COLUMN IF NOT EXISTS verified_email String DEFAULT '';
