-- A workload trust that tells agents apart (server/auth/agents.py): the claim
-- naming the agent, which gets its own subject and namespace, and the claim
-- naming its owner's email ('' on a trust for one bot). jwks_uri: where the
-- issuer's keys are published when its discovery document is not reachable
-- ('' = discovery). max_token_s: the longest a workload token may live (exp
-- less iat) to be taken. max_agents: how many agents the trust may make.
ALTER TABLE loom.machine_identities
    ADD COLUMN IF NOT EXISTS `agent_claim` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `owner_claim` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `jwks_uri` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `max_token_s` UInt32 DEFAULT 3600,
    ADD COLUMN IF NOT EXISTS `max_agents` UInt32 DEFAULT 1000;
