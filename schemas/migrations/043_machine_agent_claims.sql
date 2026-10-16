-- A workload trust that tells agents apart (server/auth/agents.py): the claim
-- naming the agent, which gets its own subject and namespace, and the claim
-- naming its owner's email ('' on a trust for one bot). jwks_uri: where the
-- issuer's keys are published when its discovery document is not reachable
-- ('' = discovery).
ALTER TABLE loom.machine_identities
    ADD COLUMN IF NOT EXISTS `agent_claim` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `owner_claim` String DEFAULT '',
    ADD COLUMN IF NOT EXISTS `jwks_uri` String DEFAULT '';
