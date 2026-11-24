CREATE TABLE IF NOT EXISTS loom.mcp_worker_refresh_admission (
    org text COLLATE "C" NOT NULL,
    namespace text COLLATE "C" NOT NULL,
    source_id uuid NOT NULL CHECK (source_id <> '00000000-0000-0000-0000-000000000000'),
    agent text COLLATE "C" NOT NULL,
    user_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    actor text NOT NULL,
    source_class text NOT NULL,
    PRIMARY KEY (org, namespace, source_id)
);

CREATE INDEX IF NOT EXISTS mcp_worker_refresh_admission_by_user
    ON loom.mcp_worker_refresh_admission (org, user_id, namespace);

ALTER TABLE loom.knowledge_source_refresh ADD COLUMN IF NOT EXISTS admission_source_id uuid;

CREATE INDEX IF NOT EXISTS knowledge_source_refresh_by_source
    ON loom.knowledge_source_refresh (org, namespace, admission_source_id, item_key)
    WHERE admission_source_id IS NOT NULL;
