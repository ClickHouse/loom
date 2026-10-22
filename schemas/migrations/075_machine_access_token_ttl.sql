-- The longest access token a machine identity's proof may be issued
-- (server/auth/machines.py): an exchange asks with `expires_in` at
-- /oauth/token and gets at most this, and a CI token's exchange never past the
-- token it hands over. 3600 for every identity made before, the hour every
-- token got; a build that does not know the column issues an hour, and writes
-- 3600 when it saves the bot.
ALTER TABLE loom.machine_identities
    ADD COLUMN IF NOT EXISTS `max_access_token_s` UInt32 DEFAULT 3600;
