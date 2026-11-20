-- The user name a loom.code_repos row's network git presents to an operator's forward
-- proxy (LOOM_CODE_GIT_PROXY_URL; server/code/app/index/git_proxy.py). Not a secret.
-- A row written before this file, or by a build before it, reads '': the deployment's
-- LOOM_CODE_GIT_PROXY_IDENTITY. Additive: a running pod of the previous build reads the
-- table by column name, writes a new row without the column and copies an existing
-- row whole, so it keeps working and keeps a value a newer pod wrote.
ALTER TABLE loom.code_repos
    ADD COLUMN IF NOT EXISTS `git_proxy_identity` String DEFAULT '';
