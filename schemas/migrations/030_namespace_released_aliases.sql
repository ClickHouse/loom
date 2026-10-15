-- The spellings (login handle, email) of the person whose account a claim release cleared.
-- The release blanks the account's owner and contact email, the pair that ties a person's
-- two spellings; the claim cap reads this column too, so a token minted for one
-- spelling stays capped at grants revoked under the other. Never read to admit anyone.
ALTER TABLE loom.namespaces
    ADD COLUMN IF NOT EXISTS `released_aliases` Array(String) DEFAULT [];
