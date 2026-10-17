-- The emails entry a SCIM user's email came from (server/auth/scim_routes.py):
-- its type, lower-cased ('' when it has none), and whether it was marked
-- primary. A PATCH filtered on an entry (`emails[type eq "work"]`) is checked
-- against the email only when it selects that entry, or one that would take
-- its place: primary, else work, else the first.
ALTER TABLE loom.scim_users
    ADD COLUMN IF NOT EXISTS `email_type` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `email_primary` UInt8 DEFAULT 0;
