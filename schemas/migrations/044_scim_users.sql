-- People an identity provider provisions over SCIM 2.0 (server/auth/scim.py).
--
-- id is the SCIM resource id, derived from email (lower-cased), so every
-- replica and every retry names a person the same. email is the address an
-- SSO sign-in carries: the provider's primary email, else its userName.
-- active = 0 is a deactivation: every subject in subjects (each one an SSO
-- sign-in with the email could reach when it was deactivated) is revoked, and
-- SSO sign-in with the email is refused until it is 1 again. deleted = 1 is a
-- SCIM DELETE: the person stays deactivated and the resource answers 404.
CREATE TABLE IF NOT EXISTS loom.scim_users
(
    `id` String,
    `user_name` String,
    `email` String,
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
