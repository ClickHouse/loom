-- What a loom.code_repos row is, which decides how far it is trusted
-- (server/code/domain/index/repos.py): 'page' a branch a member added from the
-- Repositories page, 'operator' a branch an admin of an operator entry added from
-- it, 'access' the access settings of an operator entry (code.repo.set_access).
-- A row written before this file, or by a build before it, reads ''.
ALTER TABLE loom.code_repos
    ADD COLUMN IF NOT EXISTS `row_kind` LowCardinality(String) DEFAULT '';
