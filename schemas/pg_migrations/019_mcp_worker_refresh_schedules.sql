CREATE TABLE IF NOT EXISTS loom.mcp_worker_refresh_schedules (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL CHECK (length(user_id) BETWEEN 1 AND 255 AND user_id <> '-'),
    agent text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    source_id uuid NOT NULL CHECK (source_id <> '00000000-0000-0000-0000-000000000000'),
    connection_id uuid NOT NULL CHECK (connection_id <> '00000000-0000-0000-0000-000000000000'),
    worker_id uuid NOT NULL CHECK (worker_id <> '00000000-0000-0000-0000-000000000000'),
    connection_generation bigint NOT NULL CHECK (connection_generation > 0),
    selection_generation bigint NOT NULL CHECK (selection_generation > 0),
    worker_generation bigint NOT NULL CHECK (worker_generation > 0),
    interval_seconds integer NOT NULL CHECK (interval_seconds BETWEEN 300 AND 86400),
    next_due_at timestamptz NOT NULL,
    paused boolean NOT NULL,
    PRIMARY KEY (org, namespace, source_id)
);

CREATE INDEX IF NOT EXISTS mcp_worker_refresh_schedules_due
    ON loom.mcp_worker_refresh_schedules (org, namespace, next_due_at, source_id)
    WHERE NOT paused;

CREATE INDEX IF NOT EXISTS mcp_worker_refresh_schedules_by_user
    ON loom.mcp_worker_refresh_schedules (org, user_id, namespace);
