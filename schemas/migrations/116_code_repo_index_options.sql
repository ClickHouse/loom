ALTER TABLE loom.code_repos
    ADD COLUMN IF NOT EXISTS `index_options_json` String DEFAULT '';
