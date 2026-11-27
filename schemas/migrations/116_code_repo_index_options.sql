ALTER TABLE loom.code_repos
    ADD COLUMN IF NOT EXISTS `index_options_json` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `retirement_id` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `retirement_requester` String DEFAULT '';
