-- People an identity provider provisions over SCIM 2.0 (server/auth/scim.py).
--
-- id is the SCIM resource id, derived from email (lower-cased), so every
-- replica and every retry names a person the same. email is the address an
-- SSO sign-in carries: of the emails entries with an address, the one
-- marked primary, else the first work one, else the first; with none, the
-- userName. emails is the provider's emails list as last written (JSON:
-- value, type, primary), so a PATCH is applied to it and refused when the
-- email it leaves keyed differs.
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
    `emails` String,
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
