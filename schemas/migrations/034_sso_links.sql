-- Password accounts carried over to single sign-on (server/auth/sso_links.py).
--
-- Self-signup never verifies the email it is given, so an SSO sign-in does not
-- reach a password account on an email match alone: whoever typed the address
-- first would own it. A row here says the account is the email's owner's: its
-- owner proved the password once from an SSO sign-in (linked_by 'self'), or an
-- operator vouched for its domain (`loom sso adopt`, linked_by 'operator:…').
-- An SSO sign-in with that verified email then reaches the account, its
-- subject, grants and data unchanged. linked = 0 is an unlink.
CREATE TABLE IF NOT EXISTS loom.sso_links
(
    `email` String,
    `org` LowCardinality(String),
    `namespace` LowCardinality(String),
    `linked` UInt8,
    `linked_by` String,
    `linked_at` DateTime64(3, 'UTC'),
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (email, org, namespace);
