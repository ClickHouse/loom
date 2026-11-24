CREATE SEQUENCE IF NOT EXISTS loom.mcp_worker_source_reader_generations AS bigint;

CREATE TABLE IF NOT EXISTS loom.mcp_worker_source_readers (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL CHECK (length(user_id) BETWEEN 1 AND 255 AND user_id <> '-'),
    agent text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    source_id uuid NOT NULL CHECK (source_id <> '00000000-0000-0000-0000-000000000000'),
    selection_generation bigint NOT NULL CHECK (selection_generation > 0),
    connection_id uuid NOT NULL CHECK (connection_id <> '00000000-0000-0000-0000-000000000000'),
    connection_generation bigint NOT NULL CHECK (connection_generation > 0),
    link_generation bigint NOT NULL CHECK (link_generation > 0),
    active boolean NOT NULL,
    PRIMARY KEY (org, namespace, source_id, user_id)
);

CREATE INDEX IF NOT EXISTS mcp_worker_source_readers_by_user
    ON loom.mcp_worker_source_readers (org, user_id, namespace);
