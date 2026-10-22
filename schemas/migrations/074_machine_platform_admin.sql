-- A machine identity that holds the platform_admin role (`loom bot create
-- --platform-admin`, server/auth/machines.py): every token its proofs get
-- carries the role, which spans every org. 0 for every bot made before; a
-- build that does not know the column writes 0 when it saves a bot.
ALTER TABLE loom.machine_identities
    ADD COLUMN IF NOT EXISTS `platform_admin` UInt8 DEFAULT 0;
