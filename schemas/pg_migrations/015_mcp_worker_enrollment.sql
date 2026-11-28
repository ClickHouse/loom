CREATE TABLE IF NOT EXISTS loom.mcp_worker_enrollment_challenges (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    agent text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL CHECK (length(user_id) BETWEEN 1 AND 255 AND user_id <> '-'),
    session_id text COLLATE "C" NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    worker_id uuid NOT NULL CHECK (worker_id <> '00000000-0000-0000-0000-000000000000'),
    profile_digest text COLLATE "C" NOT NULL CHECK (profile_digest ~ '^[0-9a-f]{64}$'),
    connection_id uuid NOT NULL CHECK (connection_id <> '00000000-0000-0000-0000-000000000000'),
    generation bigint NOT NULL CHECK (generation > 0),
    nonce uuid NOT NULL CHECK (nonce <> '00000000-0000-0000-0000-000000000000'),
    issued_at bigint NOT NULL CHECK (issued_at >= 0),
    expires_at bigint NOT NULL CHECK (expires_at - issued_at BETWEEN 1 AND 120),
    consumed boolean NOT NULL DEFAULT false,
    PRIMARY KEY (org, nonce)
);

CREATE INDEX IF NOT EXISTS mcp_worker_enrollment_challenges_expiry
    ON loom.mcp_worker_enrollment_challenges (org, expires_at, nonce);

CREATE INDEX IF NOT EXISTS mcp_worker_enrollment_challenges_active_connection
    ON loom.mcp_worker_enrollment_challenges (org, connection_id, expires_at) WHERE NOT consumed;

CREATE INDEX IF NOT EXISTS mcp_worker_enrollment_challenges_by_user
    ON loom.mcp_worker_enrollment_challenges (org, user_id, namespace);

CREATE TABLE IF NOT EXISTS loom.mcp_worker_accounts (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    agent text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL CHECK (length(user_id) BETWEEN 1 AND 255 AND user_id <> '-'),
    session_id text COLLATE "C" NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    worker_id uuid NOT NULL CHECK (worker_id <> '00000000-0000-0000-0000-000000000000'),
    profile_digest text COLLATE "C" NOT NULL CHECK (profile_digest ~ '^[0-9a-f]{64}$'),
    connection_id uuid NOT NULL CHECK (connection_id <> '00000000-0000-0000-0000-000000000000'),
    generation bigint NOT NULL CHECK (generation > 0),
    resource_space text COLLATE "C" CHECK (resource_space ~ '^[0-9a-f]{64}$'),
    active boolean NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (org, connection_id)
);

CREATE INDEX IF NOT EXISTS mcp_worker_accounts_by_user
    ON loom.mcp_worker_accounts (org, user_id, namespace);
