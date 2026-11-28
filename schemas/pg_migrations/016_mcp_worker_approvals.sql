CREATE SEQUENCE IF NOT EXISTS loom.mcp_worker_approval_generations AS bigint;

CREATE TABLE IF NOT EXISTS loom.mcp_worker_approvals (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    agent text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    worker_id uuid NOT NULL CHECK (worker_id <> '00000000-0000-0000-0000-000000000000'),
    profile_digest text COLLATE "C" NOT NULL CHECK (profile_digest ~ '^[0-9a-f]{64}$'),
    generation bigint NOT NULL CHECK (generation > 0),
    modes smallint NOT NULL CHECK (modes BETWEEN 0 AND 7),
    team_access boolean NOT NULL,
    active boolean NOT NULL,
    CHECK ((active AND modes > 0) OR (NOT active AND modes = 0 AND NOT team_access)),
    PRIMARY KEY (org, namespace, worker_id, profile_digest)
);

CREATE INDEX IF NOT EXISTS mcp_worker_approvals_by_user
    ON loom.mcp_worker_approvals (org, user_id, namespace);
