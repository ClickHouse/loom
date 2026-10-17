-- People an identity provider provisions over SCIM 2.0 (server/auth/scim.py).
--
-- id is the SCIM resource id, derived from user_name (the email, lower-cased),
-- so every replica and every retry names a person the same. active = 0 is a
-- deactivation: the person's subject is revoked and SSO sign-in with the email
-- is refused until it is 1 again. subject and org are the account the email
-- reached when it was deactivated ('' when it reached none). deleted = 1 is a
-- SCIM DELETE: the person stays deactivated and the resource answers 404.
CREATE TABLE IF NOT EXISTS loom.scim_users
(
    `id` String,
    `user_name` String,
    `active` UInt8,
    `deleted` UInt8,
    `subject` String,
    `org` LowCardinality(String),
    `created_at` DateTime64(3, 'UTC'),
    `updated_at` DateTime64(3, 'UTC'),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY id
SETTINGS non_replicated_deduplication_window = 1000;
