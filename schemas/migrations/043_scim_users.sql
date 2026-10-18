-- People an identity provider provisions over SCIM 2.0 (server/auth/scim.py).
--
-- id is the SCIM resource id, derived from email (lower-cased), so every
-- replica and every retry names a person the same. email is the address an
-- SSO sign-in carries: the emails entry marked primary, else the work one,
-- else the first, else the userName. email_type ('' when none) and
-- email_primary describe that entry, so a PATCH filtered on an entry
-- (`emails[type eq "work"]`) is checked against the email only when it
-- selects it or one that would take its place. email_from_user_name = 1 when
-- no entry gave the email: a userName rename would then change it.
-- active = 0 is a deactivation: subjects lists what an SSO sign-in with the
-- email could reach when it was deactivated, or since. Each one the email
-- still reaches is revoked unless an operator gave it back
-- (`loom revoke-token --scim`), and SSO sign-in with the email is refused
-- until active is 1 again. deleted = 1 is a SCIM DELETE: the person stays
-- deactivated and the resource answers 404.
CREATE TABLE IF NOT EXISTS loom.scim_users
(
    `id` String,
    `user_name` String,
    `email` String,
    `email_type` String,
    `email_primary` UInt8,
    `email_from_user_name` UInt8,
    `active` UInt8,
    `deleted` UInt8,
    `subjects` Array(String),
    `created_at` DateTime64(3, 'UTC'),
    `updated_at` DateTime64(3, 'UTC'),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY id
SETTINGS non_replicated_deduplication_window = 1000;
