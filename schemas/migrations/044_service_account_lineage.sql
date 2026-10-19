-- The subjects a token's ancestry runs through: the subject that made it and
-- each one above it (server/auth/service_accounts.py). A token's name can be
-- reused once it is revoked, and the new row replaces the old one under FINAL;
-- the pass that revokes what a subject made finds that token's children by
-- this column, not through the row their parent's name held. [] on rows
-- written before it.
ALTER TABLE loom.service_accounts
    ADD COLUMN IF NOT EXISTS `lineage` Array(String) DEFAULT [];
