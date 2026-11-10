-- loom.service_accounts.saves_to: where a list token saves the calls that name no
-- namespace, as signed into its `saves_to` claim at mint. '' for a `*` token (its
-- record is the namespace named before `*`), a token with no default target, and every
-- token minted before the column. A row rewritten whole carries it forward.
ALTER TABLE loom.service_accounts ADD COLUMN IF NOT EXISTS saves_to String DEFAULT '';
