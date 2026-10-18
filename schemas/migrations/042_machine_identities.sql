-- Bots and CI jobs that never sign in at an identity provider
-- (server/auth/machines.py, `loom bot`).
--
-- An operator registers each one with the namespaces its data belongs in and
-- how it proves itself at /oauth/token; it then gets hour-long tokens, never a
-- long-lived secret:
-- public_keys: a JWKS. The bot signs a short assertion with its private key
--   (client credentials with private_key_jwt, RFC 7523).
-- issuer, audience, claims: a workload trust. A CI job hands over the OIDC
--   token its platform issued, and Loom accepts it only from exactly that
--   issuer, for that audience, with every listed claim matching (RFC 8693).
-- ns_roles: (namespace, role) in read order, as `loom mint-token
--   --namespace-role` takes them. active = 0 is a disabled bot.
-- agent_claim: on a workload trust that tells agents apart
--   (server/auth/agents.py), the claim naming the agent, which gets its own
--   subject and namespace ('' on a trust for one bot). jwks_uri: where the
--   issuer's keys are published when its discovery document is not reachable
--   ('' = discovery). max_token_s: the longest a workload token may live (exp
--   less iat) to be taken. max_agents: how many agents the trust may make.
CREATE TABLE IF NOT EXISTS loom.machine_identities
(
    `name` String,
    `org` LowCardinality(String),
    `ns_roles` Array(Tuple(String, String)),
    `public_keys` String,
    `issuer` String,
    `audience` String,
    `claims` Map(String, String),
    `active` UInt8,
    `updated_by` String,
    `updated_at` DateTime64(3, 'UTC'),
    `version` UInt64,
    `agent_claim` String DEFAULT '',
    `jwks_uri` String DEFAULT '',
    `max_token_s` UInt32 DEFAULT 3600,
    `max_agents` UInt32 DEFAULT 1000
)
ENGINE = ReplacingMergeTree(version)
ORDER BY name
SETTINGS non_replicated_deduplication_window = 1000;
